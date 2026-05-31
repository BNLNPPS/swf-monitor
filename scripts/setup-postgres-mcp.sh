#!/usr/bin/env bash
#
# setup-postgres-mcp.sh — opt-in, per-user: give YOUR Claude Code read-only SQL
# access to the swfdb system database through the Postgres MCP server.
#
# The postgres-mcp tool is installed system-wide (/usr/local/bin); this script
# only adds, for the running user, a private 0600 credential and a user-scope
# Claude Code MCP entry. It changes nothing shared, restarts no service, and does
# nothing for anyone who does not run it. The swf-monitor bot never sees it.
#
# See docs/POSTGRES_MCP.md.
#
# Usage:
#   scripts/setup-postgres-mcp.sh            # register for you
#   scripts/setup-postgres-mcp.sh --remove   # undo (leaves the system tool)
#
set -euo pipefail

NAME=postgres-swf
SERVER=/usr/local/bin/postgres-mcp
CONN="postgresql://swf_ro@localhost:5432/swfdb"
SHARED_CRED=/data/swf-shared/swf_ro.pgpass
USER_PGPASS="$HOME/.config/swf/swf_ro.pgpass"

# ---- teardown ---------------------------------------------------------------
if [[ "${1:-}" == "--remove" ]]; then
  claude mcp remove -s user "$NAME" 2>/dev/null || true
  rm -f "$USER_PGPASS"
  echo "Removed: $NAME user-scope MCP entry and $USER_PGPASS (system tool left in place)."
  exit 0
fi

# ---- setup ------------------------------------------------------------------
command -v claude >/dev/null 2>&1 || { echo "ERROR: claude CLI not found." >&2; exit 1; }
[[ -x "$SERVER" ]] || {
  echo "ERROR: $SERVER not found. An admin installs it system-wide once — see docs/POSTGRES_MCP.md." >&2
  exit 1; }
[[ -r "$SHARED_CRED" ]] || {
  echo "ERROR: $SHARED_CRED not readable. Ask for swf_ro access." >&2; exit 1; }

# Private 0600 credential — a copy of the shared read-only line.
# libpq refuses a PGPASSFILE that is group/world-readable, so we copy rather than point.
mkdir -p "$(dirname "$USER_PGPASS")"
install -m 600 "$SHARED_CRED" "$USER_PGPASS"

# Register at USER scope: personal, never committed, bot-invisible, cwd-independent.
# --access-mode restricted = read-only plus guards against heavy/unsafe queries.
claude mcp remove -s user "$NAME" 2>/dev/null || true
claude mcp add -s user "$NAME" -e PGPASSFILE="$USER_PGPASS" -- \
  "$SERVER" --access-mode restricted "$CONN"

cat <<'EOF'

Registered postgres-swf (read-only, swfdb) at user scope.
NOTE: a mid-session `claude mcp add` is not live until you RESTART Claude Code.
After restart, `/mcp` should list postgres-swf; confirm with `list_schemas` and:
  SELECT version();
EOF
