"""Completion report for a task branch — one document built only from real data.

``adlc.py report <task_id>`` assembles what a reviewer (or the tracker comment / PR body)
needs: the commits, files changed, the recorded test run and whether it still matches the
branch HEAD, the plan that approved the work, merge-readiness blockers, and change-impact
risks. Nothing is taken from agent prose; every line comes from git, the state file or the
plan store. Output is redacted with the same rules as the rest of the CLI.

Read-only: it never mutates git, the state file, plans or the tracker.
"""

from __future__ import annotations

import os
from typing import Any, Dict

import gitutil
from config import Config, redact_text
from impact import analyze
from merge import Integrator
from plan_store import gate_status
from worktree import WorktreeManager


def build_report(config: Config, manager: WorktreeManager, task_id: str) -> Dict[str, Any]:
    record = manager.get(task_id)
    if not record:
        raise gitutil.GitError(f"No worktree record for task {task_id}")
    impact = analyze(manager, task_id)
    if record.merge_commit:   # already integrated: preflight against the target is meaningless
        class _Done:
            can_merge, ahead, behind = False, 0, 0
            blockers = [f"already merged as {record.merge_commit[:10]}"]
        pre = _Done()
    else:
        pre = Integrator(config, manager).preflight(task_id)
    v = record.validation or {}
    head = gitutil.rev_parse(record.path, "HEAD") if os.path.isdir(record.path) else None
    if head is None and gitutil.branch_exists(manager.repo, record.branch):
        head = gitutil.rev_parse(manager.repo, record.branch)
    tests = {"recorded": bool(v), "command": v.get("cmd"), "exit_code": v.get("exit_code"),
             "ran_at_head": v.get("head"), "current_head": head,
             "current": bool(v) and v.get("head") == head,
             "passed": bool(v) and v.get("exit_code") == 0}
    return {
        "task_id": record.task_id, "branch": record.branch, "base_branch": record.base_branch,
        "base_commit": record.base_commit, "state": record.state, "kind": record.kind,
        "owner_agent": record.owner_agent,
        "plan": gate_status(config, task_id, manager.repo),
        "commits": impact.commits,
        "files": {"added": impact.added, "modified": impact.modified,
                  "deleted": impact.deleted, "renamed": impact.renamed},
        "tests": tests,
        "merge_readiness": {"can_merge": pre.can_merge, "blockers": pre.blockers,
                            "behind_target": pre.behind, "ahead": pre.ahead},
        "risks": impact.risks, "potential_impact": impact.potential_impact,
        "shared_with_tasks": impact.shared_with_tasks,
        "recommended_validation": impact.recommended_validation,
    }


def render_markdown(report: Dict[str, Any], config: Config) -> str:
    r, t, m = report, report["tests"], report["merge_readiness"]
    L = [f"# Task {r['task_id']} — completion report", "",
         f"- Branch `{r['branch']}` (from `{r['base_branch']}` @ `{r['base_commit'][:10]}`)",
         f"- State: **{r['state']}**" + (f" · agent `{r['owner_agent']}`" if r["owner_agent"] else ""),
         f"- Plan: {r['plan']['plan_id'] or 'none'} — {r['plan']['reason']}", ""]
    L += ["## Tests"]
    if not t["recorded"]:
        L.append("- ⚠ No test run recorded (`adlc.py validate`).")
    else:
        verdict = "passed" if t["passed"] else f"FAILED (exit {t['exit_code']})"
        L.append(f"- `{t['command']}` {verdict}" +
                 ("" if t["current"] else " — ⚠ ran on an older commit than the branch HEAD"))
    L += ["", f"## Commits ({len(r['commits'])})"] + [f"- {c}" for c in r["commits"]]
    L += ["", "## Files changed"]
    any_files = False
    for label in ("added", "modified", "deleted", "renamed"):
        for f in r["files"][label]:
            L.append(f"- {label}: `{f}`")
            any_files = True
    if not any_files:
        L.append("- (none)")
    L += ["", "## Merge readiness",
          f"- {'Ready' if m['can_merge'] else 'Blocked'}"
          f" · {m['ahead']} ahead / {m['behind_target']} behind `{r['base_branch']}`"]
    L += [f"- Blocker: {b}" for b in m["blockers"]]
    if r["risks"]:
        L += ["", "## Risks"] + [f"- {x}" for x in r["risks"]]
    if r["shared_with_tasks"]:
        L += ["", "## Files shared with other active tasks"]
        L += [f"- `{f}`: {', '.join(ids)}" for f, ids in r["shared_with_tasks"].items()]
    L += ["", "## Recommended validation"] + [f"- {x}" for x in r["recommended_validation"]]
    return redact_text("\n".join(L) + "\n", config)
