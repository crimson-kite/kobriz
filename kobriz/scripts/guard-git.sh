#!/usr/bin/env bash
# kobriz ADLC — PreToolUse(Bash) safety guard.
#
# Reads the hook JSON payload on stdin and blocks a SMALL, high-confidence set of
# destructive git operations that could discard the user's work or bypass review:
#   - force push            (git push --force / -f / --force-with-lease to any remote)
#   - hard reset            (git reset --hard)
#   - clean with force      (git clean -f / -fd / -xf ...)
#   - branch force-delete    is allowed (reversible); NOT blocked.
#
# Exit 0 = allow (default). Exit 2 = block with the stderr message shown to Claude.
# It deliberately errs toward allowing: anything it cannot parse is allowed.

payload="$(cat 2>/dev/null)" || exit 0

# Extract the command string without requiring jq (python3 is already a dependency).
cmd="$(printf '%s' "$payload" | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get("tool_input", {}).get("command", ""))
except Exception:
    print("")
' 2>/dev/null)"

[ -z "$cmd" ] && exit 0

block() {
  echo "kobriz: blocked a potentially destructive git command ($1)." >&2
  echo "If this is intentional, run it outside kobriz or disable this guard in hooks.json." >&2
  exit 2
}

# Normalize whitespace for matching.
norm="$(printf '%s' "$cmd" | tr '\n' ' ')"

# Drop the safe --force-with-lease[=ref] form first, then look for any remaining force:
# a plain --force/-f, or a "+refspec" (git push origin +main), which forces without a flag.
push_norm="$(printf '%s' "$norm" | sed -E 's/--force-with-lease(=[^ ]*)?//g')"
case "$push_norm" in
  *git*push*--force*|*git*push*\ -f\ *|*git*push*\ -f|*git*push*\ -[a-zA-Z]*f[a-zA-Z]*\ *) block "git push --force" ;;
  *git*push*\ +[A-Za-z0-9_./-]*) block "git push with a +refspec (force)" ;;
esac

# "reset" / "clean" must be their own word (a git subcommand), not part of a file name such as
# "cleanup-file" or "reset-notes", which the old substring patterns wrongly treated as a match.
if printf '%s' "$norm" | grep -Eq 'git[^;&|]*[[:space:]]reset[[:space:]]+([^;&|]*[[:space:]])?--hard([[:space:]]|$)'; then
  block "git reset --hard"
fi

if printf '%s' "$norm" | grep -Eq 'git[^;&|]*[[:space:]]clean[[:space:]]+([^;&|]*[[:space:]])?(-[a-zA-Z]*f[a-zA-Z]*|--force)([[:space:]]|$)'; then
  block "git clean -f"
fi

exit 0
