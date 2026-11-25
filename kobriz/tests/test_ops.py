"""Post-merge loop, REST resilience, doctor/resume/retry, approval hardening, intake, PR."""

import json
import os
import stat
import subprocess
import sys
from argparse import Namespace

import pytest

import adlc
import gitutil
from config import Config
from intake_store import IntakeStore, find_similar
from merge import Integrator
from plan_store import PlanError, PlanStore
from task_client import RestTaskClient, TaskClientError, TransientError
from worktree import WorktreeManager

ADLC = os.path.join(os.path.dirname(__file__), "..", "scripts", "adlc.py")


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(path, rel, content="x = 1\n", msg="change"):
    full = os.path.join(path, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    open(full, "w").write(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", msg)


@pytest.fixture(autouse=True)
def _scrub_env():
    """_rest() sets os.environ directly; never let that leak between tests."""
    yield
    for k in ("ADLC_TASK_PAGE_PARAM", "ADLC_TASK_HTTP_RETRIES"):
        os.environ.pop(k, None)


@pytest.fixture
def env(base_env, monkeypatch):
    monkeypatch.setenv("ADLC_REQUIRE_TESTS", "false")
    return base_env


def _add_origin(repo, tmp_path):
    bare = str(tmp_path / "origin.git")
    subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
    _git(repo, "remote", "add", "origin", bare)
    return bare


# -- post-merge loop ----------------------------------------------------------------

def test_merge_completes_the_task_and_cleans_up(env):
    mgr = WorktreeManager(Config.load())
    rec = mgr.create("1", "x")
    _commit(rec.path, "app/f.py")
    res = Integrator(Config.load(), mgr).integrate("1", confirm=True)
    assert res.merged and res.state == "COMPLETED" and res.cleaned
    after = mgr.get("1")
    assert after.state == "COMPLETED" and after.merge_commit == res.merge_commit
    assert not os.path.isdir(rec.path)                      # worktree gone, record + branch kept
    assert gitutil.branch_exists(env, rec.branch)
    with pytest.raises(gitutil.GitError, match="already COMPLETED"):
        Integrator(Config.load(), mgr).integrate("1", confirm=True)


def test_failing_post_merge_tests_mark_failed_and_keep_everything(env, monkeypatch):
    monkeypatch.setenv("ADLC_POST_MERGE_TESTS", "true")
    monkeypatch.setenv("ADLC_TEST_COMMAND", f"{sys.executable} -c 'import sys; sys.exit(3)'")
    mgr = WorktreeManager(Config.load())
    rec = mgr.create("2", "x")
    _commit(rec.path, "app/f.py")
    res = Integrator(Config.load(), mgr).integrate("2", confirm=True)
    assert res.merged and res.state == "FAILED" and not res.cleaned and res.pushed is None
    assert res.post_merge["exit_code"] == 3 and any("FAILED" in n for n in res.notes)
    assert os.path.isdir(rec.path)                           # kept for debugging
    assert mgr.get("2").merge_commit == res.merge_commit
    with pytest.raises(gitutil.GitError, match="already merged"):
        mgr.retry("2")                                       # a retry cannot undo a merge


def test_push_after_merge_is_plain_and_optional(env, monkeypatch, tmp_path):
    monkeypatch.setenv("ADLC_PUSH_AFTER_MERGE", "true")
    mgr = WorktreeManager(Config.load())
    rec = mgr.create("3", "x")
    _commit(rec.path, "app/f.py")
    res = Integrator(Config.load(), mgr).integrate("3", confirm=True)
    assert res.pushed is None and any("no 'origin'" in n for n in res.notes)
    bare = _add_origin(env, tmp_path)
    rec = mgr.create("4", "y")
    _commit(rec.path, "app/g.py")
    res = Integrator(Config.load(), mgr).integrate("4", confirm=True)
    assert res.pushed is True
    assert _git(bare, "rev-parse", "main") == _git(env, "rev-parse", "main")


def test_tracker_sync_never_hides_a_merge(env, monkeypatch):
    monkeypatch.setenv("ADLC_TASK_API_BASE_URL", "http://127.0.0.1:9")   # unreachable
    monkeypatch.setenv("ADLC_TASK_HTTP_RETRIES", "0")
    out = adlc._sync_tracker(Config.load(), "9", "COMPLETED", "merged")
    assert out["status"] == "failed" and "preserved" in out["note"]
    monkeypatch.setenv("ADLC_TASK_SOURCE_TYPE", "mcp")
    monkeypatch.setenv("ADLC_MCP_SERVER", "clickup")
    assert adlc._sync_tracker(Config.load(), "9", "COMPLETED", "m")["status"] == "delegated"


# -- REST client resilience ---------------------------------------------------------

class Scripted:
    """Opener returning scripted responses in order; can set response headers."""

    def __init__(self, responses, client_ref=None):
        self.responses, self.urls, self.client_ref = list(responses), [], client_ref

    def __call__(self, request):
        self.urls.append((request.get_method(), request.full_url))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        status, body, *hdr = r
        if hdr and self.client_ref:
            self.client_ref[0]._last_headers = hdr[0]
        return status, json.dumps(body)


def _rest(base_env, responses, retries="3", **env):
    for k, v in env.items():
        os.environ[k] = v
    os.environ["ADLC_TASK_HTTP_RETRIES"] = retries
    ref, sleeps = [], []
    op = Scripted(responses, ref)
    client = RestTaskClient(Config.load(), opener=op, sleep=sleeps.append)
    ref.append(client)
    return client, op, sleeps


def test_pagination_follows_next_links_same_host_only(base_env):
    client, op, _ = _rest(base_env, [
        (200, {"tasks": [{"id": "1"}], "links": {"next": "/tasks?cursor=b"}}),
        (200, {"tasks": [{"id": "2"}], "next": "https://tasks.example.com/tasks?cursor=c"}),
        (200, {"tasks": [{"id": "3"}]})])
    assert [t.id for t in client.list_tasks()] == ["1", "2", "3"]
    client, _, _ = _rest(base_env, [(200, {"tasks": [{"id": "1"}], "next": "https://evil.test/x"})])
    with pytest.raises(TaskClientError, match="another host"):
        client.list_tasks()


def test_pagination_by_page_param_stops_on_empty_or_repeat(base_env):
    client, op, _ = _rest(base_env, [(200, [{"id": "1"}]), (200, [{"id": "2"}]), (200, [])],
                          ADLC_TASK_PAGE_PARAM="page")
    assert [t.id for t in client.list_tasks()] == ["1", "2"]
    assert op.urls[0][1].endswith("/tasks?page=1") and op.urls[1][1].endswith("page=2")
    client, op, _ = _rest(base_env, [(200, [{"id": "1"}]), (200, [{"id": "1"}])],
                          ADLC_TASK_PAGE_PARAM="page")     # provider ignores ?page → same rows
    assert [t.id for t in client.list_tasks()] == ["1"] and len(op.urls) == 2


def test_next_link_loop_is_bounded(base_env):
    loop = (200, {"tasks": [{"id": "1"}], "next": "/tasks"})
    client, op, _ = _rest(base_env, [loop] * 3)
    assert [t.id for t in client.list_tasks()] == ["1"] and len(op.urls) == 1


def test_429_honors_retry_after_and_caps_it(base_env):
    client, op, sleeps = _rest(base_env, [
        (429, {}, {"retry-after": "7"}), (429, {}, {"retry-after": "99999"}), (200, [{"id": "1"}])])
    assert [t.id for t in client.list_tasks()] == ["1"]
    assert sleeps == [7.0, 60.0]


def test_5xx_retried_for_reads_but_not_for_creates(base_env):
    client, op, sleeps = _rest(base_env, [(503, {}), (200, [{"id": "1"}])])
    assert client.list_tasks()[0].id == "1" and len(sleeps) == 1
    client, op, _ = _rest(base_env, [(500, {"error": "boom"}), (200, {"id": "9"})])
    with pytest.raises(TaskClientError, match="HTTP 500"):
        client.create_subtask("1", "child")
    assert len(op.urls) == 1                                  # a retry could duplicate the task
    client, op, _ = _rest(base_env, [(429, {}), (200, {"id": "9", "title": "child"})])
    assert client.create_subtask("1", "child").id == "9"      # 429 = not processed → safe


def test_network_errors_retry_then_give_up(base_env):
    client, op, _ = _rest(base_env, [TransientError("reset"), (200, [{"id": "1"}])])
    assert client.list_tasks()[0].id == "1"
    client, op, _ = _rest(base_env, [TransientError("down")] * 2, retries="1")
    with pytest.raises(TransientError):
        client.list_tasks()
    assert len(op.urls) == 2


# -- doctor / resume / retry --------------------------------------------------------

def test_doctor_healthy_repo_has_no_findings(env):
    import doctor
    mgr = WorktreeManager(Config.load())
    mgr.create("1", "x")
    assert doctor.diagnose(mgr) == []


def test_doctor_repairs_missing_worktree_and_merged_task(env):
    import doctor
    mgr = WorktreeManager(Config.load())
    a = mgr.create("1", "x"); _commit(a.path, "a.py")
    b = mgr.create("2", "y"); _commit(b.path, "b.py")
    _git(env, "worktree", "remove", "--force", a.path)               # deleted by hand
    _git(env, "merge", "-q", "--no-ff", b.branch, "-m", "manual merge")   # merged outside kobriz
    found = {f["code"] for f in doctor.diagnose(mgr)}
    assert {"worktree_missing", "merged_not_completed"} <= found
    fixed = doctor.diagnose(mgr, fix=True)
    assert all(f["fixed"] for f in fixed if f["code"] in ("worktree_missing", "merged_not_completed"))
    assert os.path.isdir(a.path) and mgr.get("2").state == "COMPLETED"
    assert not [f for f in doctor.diagnose(mgr) if f["severity"] != "info"]


def test_doctor_reports_orphans_and_lost_branches_without_deleting(env):
    import doctor
    mgr = WorktreeManager(Config.load())
    rec = mgr.create("1", "x")
    os.makedirs(os.path.join(mgr.worktree_root, "mystery"))
    _git(env, "worktree", "remove", "--force", rec.path)
    _git(env, "branch", "-D", rec.branch)
    codes = {f["code"] for f in doctor.diagnose(mgr, fix=True)}
    assert {"orphan_worktree", "branch_missing"} <= codes
    assert os.path.isdir(os.path.join(mgr.worktree_root, "mystery"))     # never deleted


def test_retry_budget_and_resume_plan(env, monkeypatch):
    import doctor
    monkeypatch.setenv("ADLC_AUTO_RETRIES", "1")
    mgr = WorktreeManager(Config.load())
    mgr.create("1", "x")
    with pytest.raises(gitutil.GitError, match="only FAILED"):
        mgr.retry("1")
    mgr.set_state("1", "NEEDS_REWORK")
    assert "worktree retry 1" in doctor.resume_plan(mgr, Config.load())[0]["next"]
    assert mgr.retry("1").state == "IN_PROGRESS" and mgr.get("1").retries == 1
    mgr.set_state("1", "FAILED")
    with pytest.raises(gitutil.GitError, match="budget exhausted"):
        mgr.retry("1")
    assert "needs a human" in doctor.resume_plan(mgr, Config.load())[0]["next"]


# -- approval hardening -------------------------------------------------------------

def test_cli_approve_needs_a_terminal(gated_env, monkeypatch, tmp_path):
    store = PlanStore(Config.load())
    rec = store.create({"title": "t", "approval_task_id": "900", "tasks": [{"id": "1", "title": "t"}]})
    store.record_posted(rec["plan_id"], "900", "plan review")
    e = {**os.environ, "ADLC_APPROVAL_CONFIRM": "tty"}
    proc = subprocess.run([sys.executable, ADLC, "plan", "approve", rec["plan_id"], "--hash",
                           rec["content_hash"][:12], "--by", "R"], input=rec["content_hash"][:12] + "\n",
                          capture_output=True, text=True, env=e, start_new_session=True)  # no ctty
    assert proc.returncode == 7 and "human at a terminal" in proc.stderr
    assert store.get(rec["plan_id"])["approvals"]["session"] is None


def test_tty_confirm_requires_the_exact_hash(monkeypatch):
    class FakeTTY:
        def __init__(self, reply): self.reply = reply
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def write(self, s): pass
        def flush(self): pass
        def readline(self): return self.reply
    import builtins
    real = builtins.open
    for reply, ok in (("abc123\n", True), ("ABC123\n", True), ("nope\n", False)):
        monkeypatch.setattr(builtins, "open", lambda p, *a, **k: FakeTTY(reply) if p == "/dev/tty" else real(p, *a, **k))
        assert adlc._tty_confirm("? ", "abc123") is ok


def test_provider_approval_needs_who_what_and_fresh_timing(gated_env):
    store = PlanStore(Config.load())
    rec = store.create({"title": "t", "approval_task_id": "900", "tasks": [{"id": "1", "title": "t"}]})
    store.record_posted(rec["plan_id"], "900", "plan review")
    with pytest.raises(PlanError, match="who set the status"):
        store.record_provider_approval(rec["plan_id"], "900", "plan approved")
    with pytest.raises(PlanError, match="stale approval"):
        store.record_provider_approval(rec["plan_id"], "900", "plan approved", by="R",
                                       evidence="ev", changed_at="2000-01-01T00:00:00Z")
    with pytest.raises(PlanError, match="ISO-8601"):
        store.record_provider_approval(rec["plan_id"], "900", "plan approved", by="R",
                                       evidence="ev", changed_at="yesterday")
    with pytest.raises(PlanError, match="must carry --changed-at"):
        store.record_provider_approval(rec["plan_id"], "900", "plan approved", by="R", evidence="ev")
    ok = store.record_provider_approval(rec["plan_id"], "900", "plan approved", by="R",
                                        evidence="ev", changed_at="2999-01-01T00:00:00Z")
    assert ok["approvals"]["provider"]["changed_at"] == "2999-01-01T00:00:00Z"
    # ClickUp reports epoch milliseconds; a pre-post timestamp in that form is rejected too.
    rec3 = store.create({"title": "t3", "approval_task_id": "900", "tasks": [{"id": "3", "title": "t"}]})
    store.record_posted(rec3["plan_id"], "900", "plan review")
    with pytest.raises(PlanError, match="stale approval"):
        store.record_provider_approval(rec3["plan_id"], "900", "plan approved", by="R",
                                       evidence="ev", changed_at="1000000000000")
    store.record_provider_approval(rec3["plan_id"], "900", "plan approved", by="R", evidence="ev",
                                   changed_at="32503680000000")
    # REST read-back is read by the script itself, so it needs no self-reported evidence.
    rec2 = store.create({"title": "t2", "approval_task_id": "900", "tasks": [{"id": "2", "title": "t"}]})
    store.record_posted(rec2["plan_id"], "900", "plan review")
    store.record_provider_approval(rec2["plan_id"], "900", "plan approved", source="rest")


# -- intake -------------------------------------------------------------------------

def test_find_similar_flags_likely_duplicates():
    existing = [{"id": "10", "title": "Rate limiting for login API"},
                {"id": "11", "title": "Redesign settings page"}]
    hits = find_similar("Add rate limiting to login API", existing, 0.85)
    assert [h["id"] for h in hits] == ["10"]
    assert find_similar("Fix typo", existing, 0.85) == []
    assert find_similar("x", [{"id": "1"}, "junk", {"title": ""}], 0.85) == []


def test_intake_cap_and_source_quote(base_env):
    store = IntakeStore(Config.load())
    e = store.record("m1", "One", "1", source_quote="  said   in notes " + "x" * 400, max_items=2)
    assert len(e["source_quote"]) == 200 and "  " not in e["source_quote"]
    store.record("m1", "Two", "2", max_items=2)
    store.record("m1", "One", "1", max_items=2)                       # re-record is not a new item
    with pytest.raises(ValueError, match="ADLC_INTAKE_MAX_ITEMS"):
        store.record("m1", "Three", "3", max_items=2)


def test_cli_intake_similar_reads_json(base_env, tmp_path):
    f = tmp_path / "t.json"
    f.write_text(json.dumps([{"id": "10", "title": "Rate limiting for login API"}]))
    proc = subprocess.run([sys.executable, ADLC, "intake", "similar", "--title",
                           "Add rate limiting to login API", "--existing", str(f)],
                          capture_output=True, text=True)
    assert proc.returncode == 0 and json.loads(proc.stdout)["possible_duplicate"] is True


# -- PR -----------------------------------------------------------------------------

def _fake_gh(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text('#!/bin/sh\necho "$@" > "$GH_LOG"\necho https://example.test/pr/1\n')
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(tmp_path / "gh.log"))


def test_pr_is_a_dry_run_until_confirmed(env, tmp_path, monkeypatch, capsys):
    _fake_gh(tmp_path, monkeypatch)
    bare = _add_origin(env, tmp_path)
    mgr = WorktreeManager(Config.load())
    rec = mgr.create("7", "x")
    _commit(rec.path, "app/f.py", msg="add f")
    adlc.cmd_pr(Config.load(), Namespace(task_id="7", confirm=False, draft=False))
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] and out["title"] == "[7] add f" and "completion report" in out["body"]
    assert not os.path.exists(tmp_path / "gh.log")
    assert _git(bare, "branch", "--list") == ""                      # nothing pushed
    adlc.cmd_pr(Config.load(), Namespace(task_id="7", confirm=True, draft=True))
    assert json.loads(capsys.readouterr().out)["pr_url"] == "https://example.test/pr/1"
    assert "--draft" in open(tmp_path / "gh.log").read()
    assert rec.branch in _git(bare, "branch", "--list")


def test_pr_without_gh_or_after_merge_is_refused(env, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr("shutil.which", lambda name: None)
    _add_origin(env, tmp_path)
    mgr = WorktreeManager(Config.load())
    rec = mgr.create("8", "x"); _commit(rec.path, "app/f.py")
    with pytest.raises(SystemExit):
        adlc.cmd_pr(Config.load(), Namespace(task_id="8", confirm=True, draft=False))
    Integrator(Config.load(), mgr).integrate("8", confirm=True)
    with pytest.raises(SystemExit):
        adlc.cmd_pr(Config.load(), Namespace(task_id="8", confirm=False, draft=False))


# -- the real terminal path (pseudo-terminal, not a mock) ---------------------------

def _run_in_pty(argv, env, answer, timeout=20):
    """Run argv with a controlling terminal; type ``answer`` at the first prompt."""
    import pty
    import select
    import time
    pid, fd = pty.fork()
    if pid == 0:                                   # child: has the pty as its controlling tty
        os.execvpe(argv[0], argv, env)
    out, sent, deadline = b"", False, time.time() + timeout
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if not sent and b"confirm:" in out:
                os.write(fd, (answer + "\n").encode())
                sent = True
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace")


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal")
@pytest.mark.parametrize("typed_ok,code", [(True, 0), (False, 7)])
def test_approve_prompts_on_a_real_terminal(gated_env, typed_ok, code):
    store = PlanStore(Config.load())
    rec = store.create({"title": "t", "approval_task_id": "900", "tasks": [{"id": "1", "title": "t"}]})
    store.record_posted(rec["plan_id"], "900", "plan review")
    short = rec["content_hash"][:12]
    env = {**os.environ, "ADLC_APPROVAL_CONFIRM": "tty"}
    status, out = _run_in_pty(
        [sys.executable, ADLC, "plan", "approve", rec["plan_id"], "--hash", short, "--by", "R"],
        env, short if typed_ok else "not-the-hash")
    assert "Type the version hash" in out            # the prompt was actually shown
    assert status == code, out
    approved = store.get(rec["plan_id"])["approvals"]["session"]
    assert bool(approved) is typed_ok
