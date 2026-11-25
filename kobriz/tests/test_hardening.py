"""Regression tests for merge, state-file, validation, drift, report and guard hardening."""

import json
import os
import subprocess
import sys

import pytest

FUTURE = "32503680000000"   # year 3000 in epoch ms: always after the review post

import gitutil
from config import Config
from merge import Integrator
from plan_store import PlanStore
from worktree import WorktreeManager

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "scripts")


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(path, rel, content, msg="change"):
    full = os.path.join(path, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as fh:
        fh.write(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", msg)


@pytest.fixture
def env(base_env, monkeypatch):
    monkeypatch.setenv("ADLC_REQUIRE_TESTS", "false")
    return base_env


# -- conflict detection -----------------------------------------------------------

@pytest.mark.parametrize("name", ["config.json", "notes.md", "dir with space/a b.txt"])
def test_conflict_in_any_file_type_blocks_merge(env, name):
    _commit(env, name, "base\n")
    mgr = WorktreeManager(Config.load())
    rec = mgr.create("501", "x")
    _commit(rec.path, name, "task\n")
    _commit(env, name, "main\n")
    pre = Integrator(Config.load(), mgr).preflight("501")
    assert pre.can_merge is False
    assert pre.conflicting_files == [name]


# -- merge strategies -------------------------------------------------------------

def test_rebase_strategy_never_rewrites_target_history(env, monkeypatch):
    monkeypatch.setenv("ADLC_MERGE_STRATEGY", "rebase")
    mgr = WorktreeManager(Config.load())
    rec = mgr.create("502", "x")
    _commit(rec.path, "app/feature.py", "x = 1\n", "task commit")
    _commit(env, "app/other.py", "y = 1\n", "main moved on")
    main_before = _git(env, "rev-parse", "main")
    res = Integrator(Config.load(), mgr).integrate("502", confirm=True)
    assert res.merged, res.message
    # Every commit that was on main is still an ancestor of the new main (no rewrite).
    _git(env, "merge-base", "--is-ancestor", main_before, "main")
    assert _git(env, "log", "-1", "--format=%s", "main") == "task commit"
    assert os.path.exists(os.path.join(env, "app", "other.py"))
    assert os.path.exists(os.path.join(env, "app", "feature.py"))


def test_squash_conflict_leaves_target_clean(env, monkeypatch):
    monkeypatch.setenv("ADLC_MERGE_STRATEGY", "squash")
    mgr = WorktreeManager(Config.load())
    rec = mgr.create("503", "x")
    _commit(rec.path, "README.md", "task\n")
    _commit(env, "README.md", "main\n")
    res = Integrator(Config.load(), mgr).integrate("503", confirm=True)
    assert res.merged is False
    assert _git(env, "status", "--porcelain") == ""


# -- test validation --------------------------------------------------------------

def test_require_tests_needs_a_passing_run_on_current_head(base_env, monkeypatch):
    monkeypatch.setenv("ADLC_REQUIRE_TESTS", "true")
    mgr = WorktreeManager(Config.load())
    rec = mgr.create("504", "x")
    _commit(rec.path, "app/f.py", "x = 1\n")
    integ = Integrator(Config.load(), mgr)
    assert any("none recorded" in b for b in integ.preflight("504").blockers)

    mgr.validate("504", command=f"{sys.executable} -c 'import sys; sys.exit(1)'")
    assert any("failed" in b for b in integ.preflight("504").blockers)

    mgr.validate("504", command=f"{sys.executable} -c 'pass'")
    assert integ.preflight("504").can_merge is True

    _commit(rec.path, "app/g.py", "y = 1\n")  # new commit invalidates the recorded pass
    assert any("older commit" in b for b in integ.preflight("504").blockers)


def test_validate_handles_missing_command_and_timeout(env):
    mgr = WorktreeManager(Config.load())
    mgr.create("505", "x")
    with pytest.raises(gitutil.GitError):
        mgr.validate("505")  # no command configured
    assert mgr.validate("505", command="definitely-not-a-binary").validation["exit_code"] == 127
    slow = f"{sys.executable} -c 'import time; time.sleep(5)'"
    assert mgr.validate("505", command=slow, timeout=1).validation["exit_code"] == 124


# -- state file safety ------------------------------------------------------------

def test_corrupt_state_file_is_quarantined_not_wiped(env):
    mgr = WorktreeManager(Config.load())
    mgr.create("506", "x")
    with open(mgr._state_path, "w") as fh:
        fh.write("{truncated")
    with pytest.raises(gitutil.GitError, match="moved to"):
        mgr.get("506")
    assert any(".corrupt-" in f for f in os.listdir(mgr.worktree_root))


def test_concurrent_state_updates_do_not_lose_records(env):
    import threading
    mgr = WorktreeManager(Config.load())
    mgr.create("507", "a")
    mgr.create("508", "b")
    def flip(tid):
        for i in range(25):
            mgr.set_state(tid, "IN_PROGRESS" if i % 2 else "REVIEW", note=f"n{i}")
    ts = [threading.Thread(target=flip, args=(t,)) for t in ("507", "508")]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert {r.task_id for r in mgr.records()} == {"507", "508"}


# -- plan drift -------------------------------------------------------------------

def test_branch_touching_undeclared_files_is_blocked(gated_env, monkeypatch):
    monkeypatch.setenv("ADLC_REQUIRE_TESTS", "false")
    cfg = Config.load()
    store = PlanStore(cfg)
    plan = {"title": "t", "approval_task_id": "900",
            "tasks": [{"id": "601", "title": "t", "affected_files": ["app/users.py"]}]}
    rec = store.create(plan)
    store.record_posted(rec["plan_id"], "900", "plan review")
    store.approve_session(rec["plan_id"], rec["content_hash"][:12], by="R")
    store.record_provider_approval(rec["plan_id"], "900", "plan approved", by="R", evidence="ev", changed_at=FUTURE)
    mgr = WorktreeManager(cfg)
    wt = mgr.create("601", "x")
    _commit(wt.path, "app/users.py", "ok = 1\n")
    integ = Integrator(cfg, mgr)
    assert integ.preflight("601").can_merge is True
    _commit(wt.path, "billing/charge.py", "oops = 1\n")
    pre = integ.preflight("601")
    assert pre.can_merge is False
    assert any("not covered by the approved plan" in b for b in pre.blockers)


# -- report -----------------------------------------------------------------------

def test_report_reflects_real_state(base_env, monkeypatch):
    import report
    monkeypatch.setenv("ADLC_REQUIRE_TESTS", "true")
    cfg = Config.load()
    mgr = WorktreeManager(cfg)
    rec = mgr.create("701", "x", kind="backend", owner_agent="backend-developer")
    _commit(rec.path, "app/api/users.py", "x = 1\n", "add users api")
    md = report.render_markdown(report.build_report(cfg, mgr, "701"), cfg)
    assert "No test run recorded" in md and "Blocked" in md and "app/api/users.py" in md
    mgr.validate("701", command=f"{sys.executable} -c 'pass'")
    data = report.build_report(cfg, mgr, "701")
    assert data["tests"]["passed"] and data["tests"]["current"]
    assert data["merge_readiness"]["can_merge"] is True
    with pytest.raises(gitutil.GitError):
        report.build_report(cfg, mgr, "nope")


# -- git guard --------------------------------------------------------------------

@pytest.mark.parametrize("cmd,blocked", [
    ("git push --force", True), ("git push -f origin x", True),
    ("git push origin +main", True), ("git push --force-with-lease --force", True),
    ("git push --force-with-lease origin x", False), ("git push origin main", False),
    ("git status", False),
    # harmless commands that merely contain the words clean / reset / force-ish file names
    ("git log --format=%h -- cleanup-file", False), ("git log -- reset-hard-notes.md", False),
    ("git -C /w/task-1 checkout -- app/__pycache__ tests/__pycache__", False),
    ("git clean -n", False), ("git clean --dry-run", False), ("echo clean && git status", False),
    # the real thing, in several spellings
    ("git clean -fd", True), ("git clean -n -fdx", True), ("git clean --force", True),
    ("git -C /w clean -f", True), ("git reset --hard HEAD~1", True), ("git reset --soft HEAD~1", False),
    ("ls && git clean -fd", True)])
def test_guard_git(cmd, blocked):
    proc = subprocess.run(["bash", os.path.join(SCRIPTS, "guard-git.sh")],
                          input=json.dumps({"tool_input": {"command": cmd}}),
                          capture_output=True, text=True)
    assert (proc.returncode == 2) is blocked
