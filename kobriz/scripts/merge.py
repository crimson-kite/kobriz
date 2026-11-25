"""Controlled integration / merge logic for the kobriz ADLC plugin.

This module never force-pushes, never bypasses branch protection, and never merges
unless the configured checks pass. ``preflight`` is read-only (safe to run anytime);
``integrate`` performs the actual merge into the configured target branch and is the
only mutating entry point.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

import gitutil
from config import Config, redact_text
from plan_store import plan_drift, require_approved
from worktree import WorktreeManager


@dataclass
class PreflightResult:
    task_id: str
    branch: str
    target_branch: str
    clean_worktree: bool
    diverged: bool
    conflicting_files: List[str] = field(default_factory=list)
    ahead: int = 0
    behind: int = 0
    can_merge: bool = False
    blockers: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "branch": self.branch, "target_branch": self.target_branch,
            "clean_worktree": self.clean_worktree, "diverged": self.diverged,
            "conflicting_files": self.conflicting_files, "ahead": self.ahead, "behind": self.behind,
            "can_merge": self.can_merge, "blockers": self.blockers,
        }


@dataclass
class MergeResult:
    task_id: str
    branch: str
    target_branch: str
    merged: bool
    merge_commit: Optional[str] = None
    message: str = ""
    conflicting_files: List[str] = field(default_factory=list)
    state: Optional[str] = None                      # lifecycle state recorded after the merge
    post_merge: Optional[dict] = None                # test run on the merged target, if enabled
    pushed: Optional[bool] = None                    # None = not attempted
    cleaned: bool = False                            # task worktree removed
    notes: List[str] = field(default_factory=list)   # non-fatal follow-up problems


class Integrator:
    """Serialized, check-gated integration of task branches into the target branch."""

    def __init__(self, config: Config, manager: WorktreeManager) -> None:
        self.config = config
        self.manager = manager
        self.repo = manager.repo

    def _target_checkout(self, target: str) -> Optional[str]:
        """Return the path of the worktree that currently has ``target`` checked out, if any.

        A branch can only be checked out in one worktree at a time, so we must merge in
        that worktree rather than creating a conflicting second checkout.
        """
        for wt in gitutil.list_worktrees(self.repo):
            if wt.get("branch") == target:
                return wt.get("path")
        return None

    def _detect_conflicts(self, branch: str, target: str) -> List[str]:
        """Dry-run a merge to list conflicting files without touching the working tree.

        ``git merge-tree --write-tree`` computes the merge in memory. With ``-z`` the output
        is NUL-separated: the result tree id, then one entry per conflicted path, then an
        empty field, then informational messages. Exit 0 = clean, 1 = conflicts, other = error.
        An unparseable or failed run is reported as a conflict (fail closed), never as clean.
        """
        base_code, _, _ = gitutil.run_git(self.repo, ["merge-base", target, branch], check=False)
        if base_code != 0:
            return ["(no common ancestor between task branch and target)"]
        proc = subprocess.run(
            ["git", "-C", self.repo, "merge-tree", "--write-tree", "--name-only", "-z",
             target, branch], capture_output=True, text=True, check=False)  # noqa: S603
        if proc.returncode == 0:
            return []
        if proc.returncode != 1:
            return [f"(merge-tree failed: {proc.stderr.strip()[:200]})"]
        fields = proc.stdout.split("\0")[1:]
        files: List[str] = []
        for f in fields:
            if f == "":
                break
            if f not in files:
                files.append(f)
        return files or ["(conflict reported but no paths could be parsed)"]

    def preflight(self, task_id: str) -> PreflightResult:
        """Read-only check: is this task branch safe and ready to merge?"""
        record = self.manager.get(task_id)
        if not record:
            raise gitutil.GitError(f"No worktree record for task {task_id}")
        target = self.config.base_branch
        result = PreflightResult(task_id=task_id, branch=record.branch, target_branch=target,
                                 clean_worktree=True, diverged=False)

        if os.path.isdir(record.path):
            result.clean_worktree = gitutil.is_clean(record.path)
        if not result.clean_worktree:
            result.blockers.append("Worktree has uncommitted changes — commit or discard first")

        gitutil.run_git(self.repo, ["fetch", "--all", "--quiet"], check=False)

        if not gitutil.branch_exists(self.repo, target):
            # Try origin/<target>.
            code, _, _ = gitutil.run_git(self.repo, ["rev-parse", f"origin/{target}"], check=False)
            if code != 0:
                result.blockers.append(f"Target branch {target} not found locally or on origin")
                return result
            target_ref = f"origin/{target}"
        else:
            target_ref = target

        code, out, _ = gitutil.run_git(
            self.repo, ["rev-list", "--left-right", "--count", f"{target_ref}...{record.branch}"],
            check=False,
        )
        if code == 0 and out:
            behind_str, _, ahead_str = out.partition("\t")
            try:
                result.behind = int(behind_str.strip())
                result.ahead = int(ahead_str.strip() or out.split()[-1])
            except (ValueError, IndexError):
                pass
        result.diverged = result.behind > 0

        result.conflicting_files = self._detect_conflicts(record.branch, target_ref)
        if result.conflicting_files:
            result.blockers.append(
                f"{len(result.conflicting_files)} conflicting file(s) with {target}"
            )

        if self.config.require_tests and not self.manager.validation_passed(record):
            v = record.validation
            if not v:
                result.blockers.append("Tests required but none recorded; run `adlc.py validate "
                                       f"{task_id}`")
            elif v.get("exit_code") != 0:
                result.blockers.append(f"Tests failed (exit {v.get('exit_code')}) for the recorded run")
            else:
                result.blockers.append("Tests passed on an older commit; the branch changed since — "
                                       f"re-run `adlc.py validate {task_id}`")

        if self.config.require_plan_approval:
            drift = plan_drift(self.config, task_id, self.manager.changed_files(task_id), self.repo)
            if drift:
                result.blockers.append(
                    f"{len(drift)} changed file(s) not covered by the approved plan: "
                    f"{', '.join(drift[:5])}{' …' if len(drift) > 5 else ''} — revise the plan "
                    f"and get it approved again")

        result.can_merge = not result.blockers
        return result

    def integrate(self, task_id: str, allow_conflicts: bool = False,
                  confirm: bool = False) -> MergeResult:
        """Merge, then close the loop: record the commit, run post-merge tests (optional),
        mark COMPLETED (or FAILED if the merged target fails its tests), push (optional,
        never forced) and remove the task worktree. Follow-up problems never undo a merge;
        they are reported in ``notes`` so the manager can sync the tracker truthfully."""
        record = self.manager.get(task_id)
        if record and record.state == "COMPLETED":
            raise gitutil.GitError(f"Task {task_id} is already COMPLETED "
                                   f"(merged as {record.merge_commit}); nothing to integrate")
        result = self._merge(task_id, allow_conflicts=allow_conflicts, confirm=confirm)
        if result.merged:
            self._after_merge(result)
        return result

    def _run_post_merge_tests(self, path: str) -> Optional[dict]:
        cmd = (self.config.test_command or "").strip()
        if not (self.config.post_merge_tests and cmd):
            return None
        try:
            proc = subprocess.run(shlex.split(cmd), cwd=path, capture_output=True, text=True,
                                  timeout=900, check=False)  # noqa: S603
            code, out = proc.returncode, proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            code, out = 124, "timed out after 900s"
        except FileNotFoundError as exc:
            code, out = 127, f"command not found: {exc}"
        return {"cmd": cmd, "exit_code": code, "output_tail": redact_text(out[-2000:], self.config)}

    def _after_merge(self, result: MergeResult) -> None:
        tid = result.task_id
        self.manager.update(tid, merge_commit=result.merge_commit)
        pm = result.post_merge
        if pm and pm["exit_code"] != 0:
            result.state = "FAILED"
            self.manager.set_state(tid, "FAILED", note=f"merged as {result.merge_commit} but "
                                   f"post-merge tests failed (exit {pm['exit_code']})")
            result.notes.append("Post-merge tests FAILED on the merged target. The merge was kept "
                                "(history is never rewritten): fix forward or revert deliberately. "
                                "Push and cleanup were skipped.")
            return
        result.state = "COMPLETED"
        self.manager.set_state(tid, "COMPLETED", note=f"merged as {result.merge_commit}")
        if self.config.push_after_merge:
            code, _, err = gitutil.run_git(self.repo, ["remote", "get-url", "origin"], check=False)
            if code != 0:
                result.notes.append("Push skipped: no 'origin' remote")
            else:
                code, _, err = gitutil.run_git(
                    self.repo, ["push", "origin", result.target_branch], check=False)
                result.pushed = code == 0
                if code != 0:
                    result.notes.append(f"Push failed (merge is local only): {err[:200]}")
        if self.config.cleanup_after_merge:
            try:
                result.cleaned = self.manager.remove(tid, keep_record=True)
            except gitutil.GitError as exc:
                result.notes.append(f"Worktree kept: {exc}")

    def _merge(self, task_id: str, allow_conflicts: bool = False,
               confirm: bool = False) -> MergeResult:
        """Merge the task branch into the target branch after preflight passes.

        Mutating. Requires ``confirm=True`` OR ``config.auto_merge``, and an approved plan
        covering the task when ADLC_REQUIRE_PLAN_APPROVAL is on. Never force-pushes.
        Merges into a dedicated integration checkout, not the user's main working tree.
        """
        if not (confirm or self.config.auto_merge):
            raise gitutil.GitError(
                "Refusing to merge: pass confirm=True or set ADLC_AUTO_MERGE=true"
            )
        require_approved(self.config, task_id, repo=self.repo)
        record = self.manager.get(task_id)
        if not record:
            raise gitutil.GitError(f"No worktree record for task {task_id}")
        target = self.config.base_branch

        pre = self.preflight(task_id)
        if not pre.can_merge and not allow_conflicts:
            return MergeResult(
                task_id=task_id, branch=record.branch, target_branch=target, merged=False,
                message="Preflight failed: " + "; ".join(pre.blockers),
                conflicting_files=pre.conflicting_files,
            )

        if not gitutil.branch_exists(self.repo, target):
            return MergeResult(task_id, record.branch, target, merged=False,
                               message=f"Target branch {target} does not exist locally; "
                                       f"create/track it before integrating")

        # A branch can only be checked out in one worktree. If the target is already
        # checked out somewhere, merge there (requiring it be clean so we never clobber
        # uncommitted user work). Otherwise create a throwaway worktree for the merge.
        existing = self._target_checkout(target)
        temp_worktree = None
        if existing:
            if not gitutil.is_clean(existing):
                return MergeResult(
                    task_id, record.branch, target, merged=False,
                    message=f"Target branch {target} is checked out with uncommitted changes "
                            f"at {existing}; refusing to merge into a dirty worktree")
            integ_path = existing
        else:
            integ_path = gitutil.validate_path(
                os.path.join(self.manager.worktree_root, f"_integrate-{gitutil.slugify(task_id)}")
            )
            if os.path.exists(integ_path):
                gitutil.run_git(self.repo, ["worktree", "remove", "--force", integ_path], check=False)
            gitutil.run_git(self.repo, ["worktree", "add", integ_path, target])
            temp_worktree = integ_path
        try:
            strat = self.config.merge_strategy
            if strat == "rebase":
                # Rebase the TASK branch onto the target (in its own worktree), then
                # fast-forward the target. Never rebase the target itself.
                if not os.path.isdir(record.path):
                    return MergeResult(task_id, record.branch, target, merged=False,
                                       message="Task worktree is missing; cannot rebase safely")
                code, _, err = gitutil.run_git(record.path, ["rebase", target], check=False)
                if code != 0:
                    gitutil.run_git(record.path, ["rebase", "--abort"], check=False)
                    return MergeResult(task_id, record.branch, target, merged=False,
                                       message=f"Rebase conflict: {err[:300]}",
                                       conflicting_files=pre.conflicting_files)
                code, _, err = gitutil.run_git(integ_path, ["merge", "--ff-only", record.branch],
                                               check=False)
                if code != 0:
                    return MergeResult(task_id, record.branch, target, merged=False,
                                       message=f"Fast-forward failed after rebase: {err[:300]}")
                merge_commit = gitutil.rev_parse(integ_path, "HEAD")
            else:
                args = ["merge", "--no-ff"]
                if strat == "squash":
                    args = ["merge", "--squash"]
                args += [record.branch, "-m"] if strat != "squash" else [record.branch]
                if strat != "squash":
                    args.append(f"kobriz: integrate task {task_id} ({record.branch})")
                code, _, err = gitutil.run_git(integ_path, args, check=False)
                if code != 0:
                    # --squash leaves no MERGE_HEAD, so --abort can fail; reset --merge restores
                    # the index and working tree either way without touching other changes.
                    if gitutil.run_git(integ_path, ["merge", "--abort"], check=False)[0] != 0:
                        gitutil.run_git(integ_path, ["reset", "--merge"], check=False)
                    return MergeResult(task_id, record.branch, target, merged=False,
                                       message=f"Merge conflict: {err[:300]}",
                                       conflicting_files=pre.conflicting_files)
                if strat == "squash":
                    gitutil.run_git(integ_path, ["commit", "-m",
                                    f"kobriz: integrate task {task_id} ({record.branch})"])
                merge_commit = gitutil.rev_parse(integ_path, "HEAD")

            return MergeResult(task_id, record.branch, target, merged=True,
                               merge_commit=merge_commit,
                               message=f"Merged {record.branch} into {target} ({strat})",
                               post_merge=self._run_post_merge_tests(integ_path))
        finally:
            # Only clean up a worktree we created; never remove the user's checkout.
            if temp_worktree:
                gitutil.run_git(self.repo, ["worktree", "remove", "--force", temp_worktree], check=False)
