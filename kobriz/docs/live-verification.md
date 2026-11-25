# Live verification checklist (Phase 0)

Everything below needs a real ClickUp / Granola sign-in, so it cannot be covered by pytest.
Run it once against a **throwaway ClickUp list** and **one test Granola note**, and record the
result next to each line. Until it is done, treat every tool name and behavior as an assumption.

## Connect
- [ ] ClickUp MCP connects; note its server URL and sign-in method.
- [ ] Granola MCP connects (claude.ai connector or direct); note the tool-name prefix.
- [ ] `adlc.py setup status` and `config test` pass for the ClickUp attachment.

## ClickUp
- [ ] `clickup_get_workspace_hierarchy` returns the spaces/folders/lists the setup picker needs.
- [ ] `clickup_create_task` with `parent` creates a **subtask** (not a sibling).
- [ ] Per-list statuses read via `clickup_get_list`; the status map covers every one used.
- [ ] A status change shows **who** changed it and **when** (needed for `--by` / `--changed-at`).
- [ ] `clickup_create_comment` posts the plan and the completion report.
- [ ] 429 behavior: confirm the `Retry-After` header is sent (the REST client assumes seconds).

## Granola
- [ ] Read tools return notes, attendees and (paid plans) transcripts; note which tool returns action items.
- [ ] Write tools exist on the connected server and the guard hook blocks them (try one; expect "blocked").

## Hooks (the big unknowns)
- [ ] A command typed with `!` in Claude Code: does it reach `PreToolUse` hooks? (The guard assumes not.)
- [ ] Does a `!` command have a controlling terminal? `adlc.py plan approve` needs one with
      `ADLC_APPROVAL_CONFIRM=tty`. If not, run it in a separate terminal, or document that.
- [ ] `PreToolUse` matcher `mcp__.*` fires for the ClickUp and Granola tools.

## End to end
- [ ] `/kobriz:intake` on the test meeting → drafts → approve → tasks created, ledger recorded, re-run creates nothing.
- [ ] `/kobriz:plan` → approve in the tracker and at the terminal → `/kobriz:execute-plan`.
- [ ] `adlc.py validate`, `report`, `merge integrate --confirm`: task `COMPLETED`, tracker synced, worktree gone.
- [ ] Kill a run midway, then `adlc.py doctor` and `adlc.py resume` describe the situation correctly.

## Results (live run, 2026-11-24, Claude Code 2.1.292, sandbox space/list)

| Item | Result |
|---|---|
| ClickUp + Granola connect | Pass. ClickUp `https://mcp.clickup.com/mcp` (OAuth, prefix `mcp__clickup__`); Granola via the claude.ai connector. |
| `setup status` / `config test` | Pass after `/kobriz:setup`. The ".mcp.json doesn't declare the server" note was misleading wording (servers added with `claude mcp add` are stored elsewhere); reworded. |
| `get_workspace_hierarchy`, `get_list` statuses, `create_comment` | Pass. ClickUp stores comments as plain text (tables are flattened). |
| `create_task` with `parent` makes a subtask | **Not tested live** (only top-level tasks were created). |
| Who/when for a status change | **When:** yes (`since` in `get_task_time_in_status`, epoch ms). **Who:** no tool shows it; the signed-in account authors both agent and human actions. Tracker approval therefore proves *when*, not *who*: `--changed-at` is now required, `--by` is the approver's name asked of the user. The terminal hash is the stronger gate. |
| 429 `Retry-After` | Cannot be observed through MCP; covered by the REST mock tests only. |
| Granola read tools | Pass. No dedicated action-items tool: items come from notes/summary (as designed). |
| Granola write blocked by guard | Pass (`update_meeting_title` blocked) - hooks fire on claude.ai connector tools. |
| Guard fires on ClickUp (`mcp__.*`) | Not directly observed. Test: attempt to set the approval status through the ClickUp tool; expect a block. |
| Injection line in meeting notes | Pass: never acted on; shown as flagged and declined by the user. |
| Intake: approve, create, ledger, re-run creates nothing | Pass. |
| Plan approved in tracker + terminal, then execute | Pass. |
| `validate`, `merge integrate`, COMPLETED, tracker synced, worktree removed | Pass for both tasks. |
| `!` command has a controlling terminal | **Unknown.** An earlier "no terminal" result was caused by a bug in `_tty_confirm` (fixed; real pseudo-terminal test added). Re-test. |
| `!` commands reach PreToolUse hooks | Unknown (inferred "no" from one run). Test with a `!` command the guard would block. |
| Kill a run midway, then `doctor` / `resume` | **Not done yet.** |

### Bugs found by the live run (all fixed, with regression tests)
- `_tty_confirm` raised `UnsupportedOperation` (single `/dev/tty` handle for write+read) and reported it as "no terminal".
- Sandbox setup committed `__pycache__` (my mistake) which dirtied worktrees; sandbox now ignores it.
- `guard-git.sh` matched the substring `clean...-...f` (e.g. `cleanup-file`); `clean`/`reset` must now be a whole word.

### Still open
Subtask-with-`parent`, guard on ClickUp tools, `!` terminal/hook behavior, kill-and-recover (`doctor`/`resume`).
