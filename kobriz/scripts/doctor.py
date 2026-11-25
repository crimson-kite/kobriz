"""Reconcile kobriz's local state with git, and say what to do next.

``doctor`` compares the worktree state file with ``git worktree list``, the branches and the
disk, and reports drift (crashed runs, hand-deleted worktrees, merged-but-not-completed
tasks). It only ever *reports* unless ``fix=True``, and the fixes are deliberately limited
to non-destructive ones: re-attach a missing worktree to its existing branch, mark a task
COMPLETED when its commits are already in the base branch, and ``git worktree prune``.
It never deletes a worktree, a branch, or any uncommitted work.

``resume_plan`` is read-only: for each unfinished task, the next command to run.
"""

from __future__ import annotations

import os
from typing import Dict, List

import gitutil
from config import ALL_STATES, Config
from worktree import WorktreeManager

_RETRYABLE = ("FAILED", "NEEDS_REWORK", "MERGE_CONFLICT")


def _finding(sev: str, task_id: str, code: str, message: str, fixed: bool = False) -> Dict:
    return {"severity": sev, "task_id": task_id, "code": code, "message": message, "fixed": fixed}


def _registered_paths(repo: str) -> Dict[str, dict]:
    return {os.path.realpath(w["path"]): w for w in gitutil.list_worktrees(repo)}


def diagnose(manager: WorktreeManager, fix: bool = False) -> List[Dict]:
    repo, base = manager.repo, manager.config.base_branch
    findings: List[Dict] = []
    registered = _registered_paths(repo)
    known_paths = set()

    for rec in manager.records():
        tid = rec.task_id
        real = os.path.realpath(rec.path)
        known_paths.add(real)
        done = rec.state == "COMPLETED"
        branch_ok = gitutil.branch_exists(repo, rec.branch)
        if rec.state not in ALL_STATES:
            findings.append(_finding("error", tid, "unknown_state",
                                     f"state {rec.state!r} is not a lifecycle state"))
        if not branch_ok and not done:
            findings.append(_finding("error", tid, "branch_missing",
                                     f"branch {rec.branch} no longer exists; the task's commits "
                                     f"may be lost — check `git reflog`"))
            continue
        if not os.path.isdir(rec.path):
            if done:
                continue  # normal: the worktree was cleaned up after the merge
            fixed = False
            if fix:
                code, _, _ = gitutil.run_git(repo, ["worktree", "add", rec.path, rec.branch],
                                             check=False)
                fixed = code == 0
            findings.append(_finding("warn", tid, "worktree_missing",
                                     f"{rec.path} is gone but branch {rec.branch} exists; re-attach "
                                     f"with `git worktree add {rec.path} {rec.branch}`", fixed))
        elif real not in registered:
            findings.append(_finding("error", tid, "not_registered",
                                     f"{rec.path} exists but git does not know it as a worktree"))
        elif not done and gitutil.is_clean(rec.path) is False and rec.state in (
                "READY_TO_MERGE", "VALIDATING", "REVIEW"):
            findings.append(_finding("warn", tid, "dirty_worktree",
                                     f"{rec.state} but the worktree has uncommitted changes"))
        if branch_ok and not done and not rec.merge_commit:
            ahead = gitutil.run_git(repo, ["rev-list", "--count", f"{rec.base_commit}..{rec.branch}"],
                                    check=False)[1]
            merged = gitutil.run_git(repo, ["merge-base", "--is-ancestor", rec.branch, base],
                                     check=False)[0] == 0
            if merged and ahead.isdigit() and int(ahead) > 0:
                fixed = False
                if fix:
                    manager.update(tid, merge_commit=gitutil.rev_parse(repo, base))
                    manager.set_state(tid, "COMPLETED", note="doctor: commits already in base branch")
                    fixed = True
                findings.append(_finding("warn", tid, "merged_not_completed",
                                         f"{rec.branch} is already in {base} but state is {rec.state}",
                                         fixed))

    root = manager.worktree_root
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            full = os.path.realpath(os.path.join(root, name))
            if os.path.isdir(full) and not name.startswith(".") and full not in known_paths:
                findings.append(_finding("warn", "", "orphan_worktree",
                                         f"{full} is not tracked by kobriz; inspect it, then remove "
                                         f"it yourself if it is leftover"))
    stale = [p for p in registered if not os.path.isdir(p)]
    if stale:
        fixed = False
        if fix:
            fixed = gitutil.run_git(repo, ["worktree", "prune"], check=False)[0] == 0
        findings.append(_finding("info", "", "prunable_worktrees",
                                 f"{len(stale)} git worktree entr(y/ies) point at missing folders", fixed))
    return findings


def resume_plan(manager: WorktreeManager, config: Config) -> List[Dict]:
    """Next action for every task that is not finished. Read-only."""
    out: List[Dict] = []
    py = "python3 ${CLAUDE_PLUGIN_ROOT}/scripts/adlc.py"
    for r in manager.records():
        tid, s = r.task_id, r.state
        if s in ("COMPLETED", "CANCELLED"):
            continue
        if s in ("ASSIGNED", "IN_PROGRESS"):
            action = (f"continue implementation in {r.path} (agent: {r.owner_agent or 'unassigned'}); "
                      f"then `{py} validate {tid}`")
        elif s in ("REVIEW", "VALIDATING"):
            action = f"`{py} validate {tid}`, then `{py} report {tid}`"
        elif s == "READY_TO_MERGE":
            action = f"`{py} merge preflight {tid}`, then `{py} merge integrate {tid} --confirm`"
        elif s in _RETRYABLE and not r.merge_commit:
            if r.retries < config.auto_retries:
                action = (f"`{py} worktree retry {tid}` ({r.retries}/{config.auto_retries} retries used)"
                          if s != "MERGE_CONFLICT" else
                          f"resolve conflicts in {r.path}, `{py} worktree retry {tid}`, then re-run preflight")
            else:
                action = (f"needs a human: retry budget used ({r.retries}/{config.auto_retries}); "
                          f"inspect {r.path} and {r.note or 'the last report'}")
        elif s == "FAILED" and r.merge_commit:
            action = (f"merged as {r.merge_commit[:10]} but post-merge tests failed: fix forward or "
                      f"revert deliberately (needs a human)")
        else:
            action = f"state {s}: needs a human decision ({r.note})"
        out.append({"task_id": tid, "state": s, "branch": r.branch, "next": action})
    return out
