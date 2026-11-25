# Kobriz

Kobriz is a Claude Code plugin that turns tickets from your existing task tracker into reviewed,
merged code. Agents implement the work. Deterministic scripts decide what may run and what may
merge, and a person approves the plan and the merge.

- **Tracker stays the source of truth for tasks** (ClickUp or Jira through MCP, or any REST API).
- **Git stays the source of truth for code.** Every task runs in its own worktree and branch.
- **Humans decide.** A hash-bound plan needs two approvals, and merges need explicit confirmation.

## Where things are

| Path | What it holds |
| --- | --- |
| `kobriz/README.md` | Full documentation: setup, commands, flow, safety guarantees |
| `kobriz/scripts/` | The command-line tool `adlc.py` and the modules behind every gate |
| `kobriz/agents/`, `skills/`, `commands/`, `hooks/` | The Claude Code plugin: 3 agents, 11 skills, 8 commands, hook guards |
| `kobriz/tests/` | 179 automated tests (no network, no real credentials) |
| `kobriz/docs/live-verification.md` | What was tested against a real tracker, and what is still open |

## Run the tests

```bash
cd kobriz
python3 -m pip install pytest
python3 -m pytest tests -q
```

Needs Python 3 and git. The plugin itself uses only the Python standard library.

## Limits

Four live checks are still open, and the tracker cannot show who changed a status. These and
other limits are listed in `kobriz/docs/live-verification.md` and `kobriz/README.md`.
