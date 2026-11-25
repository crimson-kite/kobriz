"""Git worktree lifecycle management for the kobriz ADLC plugin.

Each independently-executable task gets its own isolated worktree + feature branch.
Minimal local execution metadata (task id, branch, path, base commit, owner agent,
state) is kept in a JSON state file under the worktree root so the manager can recover
and so ``/kobriz:status`` can report accurately. Git remains the source of truth for
code; the external provider remains the source of truth for task details.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

import gitutil
from config import Config, atomic_write_json, file_lock
from plan_store import require_approved

_STATE_FILENAME = ".kobriz-state.json"


@dataclass
class WorktreeRecord:
    task_id: str
    branch: str
    path: str
    base_branch: str
    base_commit: str
    kind: Optional[str] = None       # backend | frontend | ...
    owner_agent: Optional[str] = None
    state: str = "ASSIGNED"          # logical lifecycle state
    note: str = ""
    merge_commit: Optional[str] = None  # set when the branch was integrated
    retries: int = 0                  # times `worktree retry` re-opened this task
    validation: Optional[dict] = None  # last `adlc.py validate` result: cmd, exit_code, head, at


class WorktreeManager:
    """Creates, tracks and tears down task worktrees with safety guards."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.repo = gitutil.repo_root(config.repository_root or os.getcwd())
        self.worktree_root = gitutil.validate_path(
            config.worktree_root or os.path.join(os.path.dirname(self.repo),
                                                 f"{os.path.basename(self.repo)}-kobriz-worktrees")
        )
        self._state_path = os.path.join(self.worktree_root, _STATE_FILENAME)

    # -- state file -------------------------------------------------------------

    def _load_state(self) -> Dict[str, dict]:
        if not os.path.exists(self._state_path):
            return {}
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("state file is not a JSON object")
            return data
        except (json.JSONDecodeError, ValueError):
            # Never fall back to {}: the next save would silently erase every record.
            quarantine = f"{self._state_path}.corrupt-{int(time.time())}"
            os.replace(self._state_path, quarantine)
            raise gitutil.GitError(
                f"Worktree state file was unreadable and was moved to {quarantine}. "
                f"Worktrees on disk are intact; inspect the file and restore or recreate "
                f"the records before continuing")
        except OSError as exc:
            raise gitutil.GitError(f"Cannot read worktree state {self._state_path}: {exc}")

    def _save_state(self, state: Dict[str, dict]) -> None:
        os.makedirs(self.worktree_root, exist_ok=True)
        atomic_write_json(self._state_path, state)

    def records(self) -> List[WorktreeRecord]:
        return [WorktreeRecord(**v) for v in self._load_state().values()]

    def get(self, task_id: str) -> Optional[WorktreeRecord]:
        raw = self._load_state().get(str(task_id))
        return WorktreeRecord(**raw) if raw else None

    def _put(self, record: WorktreeRecord) -> None:
        with file_lock(self._state_path):
            state = self._load_state()
            state[record.task_id] = asdict(record)
            self._save_state(state)

    # -- branch naming ----------------------------------------------------------

    def branch_name(self, task_id: str, slug: str, kind: Optional[str] = None) -> str:
        parts = [self.config.branch_prefix, f"task-{gitutil.slugify(task_id)}"]
        if kind:
            parts.append(gitutil.slugify(kind, 12))
        parts.append(gitutil.slugify(slug))
        name = "/".join([parts[0], "-".join(parts[1:])])
        gitutil.validate_branch_name(name)
        return name

    # -- lifecycle --------------------------------------------------------------

    def create(self, task_id: str, slug: str, kind: Optional[str] = None,
               owner_agent: Optional[str] = None, base_branch: Optional[str] = None) -> WorktreeRecord:
        """Create an isolated worktree + feature branch for a task.

        Safety: refuses unless an approved plan covers the task (when
        ADLC_REQUIRE_PLAN_APPROVAL is on); refuses to touch the main checkout's uncommitted
        changes (it operates in a *separate* worktree); refuses to reuse an existing task
        worktree.
        """
        task_id = str(task_id)
        require_approved(self.config, task_id, repo=self.repo)
        existing = self.get(task_id)
        if existing and os.path.isdir(existing.path):
            raise gitutil.GitError(
                f"Worktree for task {task_id} already exists at {existing.path}; "
                f"remove it before recreating"
            )

        base = base_branch or self.config.base_branch
        # Resolve the base revision without disturbing the user's checkout.
        gitutil.run_git(self.repo, ["fetch", "--all", "--quiet"], check=False)
        if gitutil.branch_exists(self.repo, base):
            base_commit = gitutil.rev_parse(self.repo, base)
        else:
            # Fall back to origin/<base> or current HEAD; never reset the user's tree.
            code, out, _ = gitutil.run_git(self.repo, ["rev-parse", f"origin/{base}"], check=False)
            base_commit = out if code == 0 else gitutil.rev_parse(self.repo, "HEAD")

        branch = self.branch_name(task_id, slug, kind)
        if gitutil.branch_exists(self.repo, branch):
            raise gitutil.GitError(f"Branch {branch} already exists; pick a different task slug")

        path = gitutil.validate_path(os.path.join(self.worktree_root, f"task-{gitutil.slugify(task_id)}"))
        if os.path.exists(path):
            raise gitutil.GitError(f"Worktree path already exists: {path}")

        os.makedirs(self.worktree_root, exist_ok=True)
        # Create a new branch at base_commit AND check it out into a new worktree atomically.
        gitutil.run_git(self.repo, ["worktree", "add", "-b", branch, path, base_commit])

        record = WorktreeRecord(
            task_id=task_id, branch=branch, path=path, base_branch=base,
            base_commit=base_commit, kind=kind, owner_agent=owner_agent, state="ASSIGNED",
        )
        self._put(record)
        return record

    def update(self, task_id: str, **fields) -> Optional[WorktreeRecord]:
        """Atomically set arbitrary record fields (e.g. merge_commit, retries)."""
        with file_lock(self._state_path):
            all_state = self._load_state()
            raw = all_state.get(str(task_id))
            if not raw:
                return None
            raw.update(fields)
            all_state[str(task_id)] = raw
            self._save_state(all_state)
        return WorktreeRecord(**raw)

    def set_state(self, task_id: str, state: str, note: str = "") -> Optional[WorktreeRecord]:
        with file_lock(self._state_path):
            raw = self._load_state().get(str(task_id))
            if not raw:
                return None
            record = WorktreeRecord(**raw)
            record.state = state
            if note:
                record.note = note
            all_state = self._load_state()
            all_state[record.task_id] = asdict(record)
            self._save_state(all_state)
        return record

    def validate(self, task_id: str, command: Optional[str] = None,
                 timeout: int = 900) -> WorktreeRecord:
        """Run the test command in the task worktree and record the result against HEAD.

        The command runs without a shell. The stored result is only trusted while the
        branch HEAD still equals the commit it ran against, so a later commit invalidates it.
        """
        record = self.get(task_id)
        if not record:
            raise gitutil.GitError(f"No worktree record for task {task_id}")
        cmd = (command or self.config.test_command or "").strip()
        if not cmd:
            raise gitutil.GitError("No test command: pass --cmd or set ADLC_TEST_COMMAND")
        if not os.path.isdir(record.path):
            raise gitutil.GitError(f"Worktree {record.path} is missing; cannot run tests")
        head = gitutil.rev_parse(record.path, "HEAD")
        argv = shlex.split(cmd)
        try:
            proc = subprocess.run(argv, cwd=record.path, capture_output=True, text=True,
                                  timeout=timeout, check=False)  # noqa: S603
            code, out = proc.returncode, (proc.stdout + proc.stderr)
        except subprocess.TimeoutExpired:
            code, out = 124, f"timed out after {timeout}s"
        except FileNotFoundError as exc:
            code, out = 127, f"command not found: {exc}"
        from config import now_iso, redact_text
        record.validation = {"cmd": cmd, "exit_code": code, "head": head, "at": now_iso(),
                             "output_tail": redact_text(out[-2000:], self.config)}
        self._put(record)
        return record

    def validation_passed(self, record: WorktreeRecord) -> bool:
        """True only if tests passed on the branch's *current* HEAD."""
        v = record.validation or {}
        if v.get("exit_code") != 0 or not os.path.isdir(record.path):
            return False
        return v.get("head") == gitutil.rev_parse(record.path, "HEAD")

    def retry(self, task_id: str, note: str = "") -> WorktreeRecord:
        """Re-open a failed task for another attempt, within the ADLC_AUTO_RETRIES budget."""
        record = self.get(task_id)
        if not record:
            raise gitutil.GitError(f"No worktree record for task {task_id}")
        if record.merge_commit:
            raise gitutil.GitError(f"Task {task_id} was already merged ({record.merge_commit[:10]}); "
                                   f"a retry would not undo it — fix forward deliberately")
        if record.state not in ("FAILED", "NEEDS_REWORK", "MERGE_CONFLICT"):
            raise gitutil.GitError(f"Task {task_id} is {record.state}; only FAILED, NEEDS_REWORK "
                                   f"or MERGE_CONFLICT tasks can be retried")
        if record.retries >= self.config.auto_retries:
            raise gitutil.GitError(
                f"Retry budget exhausted for task {task_id} ({record.retries}/"
                f"{self.config.auto_retries}); a human must decide (raise ADLC_AUTO_RETRIES to allow more)")
        self.update(task_id, retries=record.retries + 1, state="IN_PROGRESS", validation=None,
                    note=note or f"retry {record.retries + 1}/{self.config.auto_retries}")
        return self.get(task_id)

    def changed_files(self, task_id: str) -> List[str]:
        """Files changed on the task branch relative to its base commit."""
        record = self.get(task_id)
        if not record:
            return []
        _, out, _ = gitutil.run_git(
            self.repo, ["diff", "--name-only", f"{record.base_commit}...{record.branch}"], check=False
        )
        return [f for f in out.splitlines() if f]

    def detect_shared_files(self) -> Dict[str, List[str]]:
        """Return {filepath: [task_ids]} for files touched by more than one active task.

        Used to avoid treating tasks as independent just because they have different IDs.
        """
        touched: Dict[str, List[str]] = {}
        for rec in self.records():
            for f in self.changed_files(rec.task_id):
                touched.setdefault(f, []).append(rec.task_id)
        return {f: ids for f, ids in touched.items() if len(ids) > 1}

    def remove(self, task_id: str, force: bool = False, keep_record: bool = False) -> bool:
        """Remove a task worktree. Honors keep-failed-worktrees retention unless forced.

        Refuses to remove a worktree that has uncommitted changes unless ``force``.
        """
        record = self.get(task_id)
        if not record:
            return False

        failed_states = {"FAILED", "NEEDS_REWORK", "MERGE_CONFLICT", "BLOCKED"}
        if (not force) and self.config.keep_failed_worktrees and record.state in failed_states:
            raise gitutil.GitError(
                f"Refusing to remove worktree for task {task_id} in state {record.state} "
                f"(ADLC_KEEP_FAILED_WORKTREES=true). Use --force to override."
            )

        if os.path.isdir(record.path):
            if (not force) and not gitutil.is_clean(record.path):
                raise gitutil.GitError(
                    f"Worktree {record.path} has uncommitted changes; use --force to remove"
                )
            gitutil.run_git(self.repo, ["worktree", "remove", record.path,
                                       *(["--force"] if force else [])], check=False)
        # Prune the state entry.
        with file_lock(self._state_path):
            state = self._load_state()
            if keep_record and task_id in state:
                state[task_id]["note"] = "worktree removed after merge; branch kept"
            else:
                state.pop(task_id, None)
            self._save_state(state)
        return True
