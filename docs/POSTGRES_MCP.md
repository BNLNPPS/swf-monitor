# Postgres MCP — read-only swfdb access for Claude Code

A Claude Code session occasionally needs to read the `swfdb` system database
directly — to answer a question no purpose-built tool covers. The Postgres MCP
server ([crystaldba/postgres-mcp](https://github.com/crystaldba/postgres-mcp))
provides that as read-only SQL plus schema-inspection tools. It complements the
curated MCP tools (`swf_*`, `pcs_*`, `panda_*`); it does not replace them.

This is for **Claude Code**, not the swf-monitor bot. The bot loads its own fixed
server list; this server is never visible to it.

The server binary is installed system-wide; each user opts in with one script that
touches only their own environment.

## Opt in

```bash
swf-monitor/scripts/setup-postgres-mcp.sh        # register for you
# ... restart Claude Code ...
```

The script copies the read-only credential to a private `~/.config/swf/swf_ro.pgpass`
(mode 0600) and registers the server with Claude Code at **user scope**. The only
prerequisite is the `claude` CLI; the `postgres-mcp` tool is already system-wide.
A mid-session registration is not live until Claude Code restarts; afterwards
`/mcp` lists `postgres-swf`. Confirm with `list_schemas` and `SELECT version();`.

## What it touches — and what it doesn't

Running the script affects only the running user: one private credential file and
one user-scope Claude Code entry. It is opt-in — a user who does not run it is
wholly unaffected — and reversible:

```bash
swf-monitor/scripts/setup-postgres-mcp.sh --remove
```

Nothing shared is reconfigured. No service restarts. swfdb authentication for the
monitor, bots, agents, and deploy is unchanged: the MCP is one additional
read-only client, not a change to how anyone else connects.

## Under the hood

- **Role `swf_ro`** on swfdb — `SELECT`-only, `default_transaction_read_only = on`,
  `statement_timeout = 15s`. Created once as shared infrastructure; serves every
  opt-in user. Default privileges are scoped to the migration role so future
  tables stay readable. The role is purely additive — it alters no existing grant.
- **Credential** at `/data/swf-shared/swf_ro.pgpass`, in libpq `.pgpass` format.
  The setup script copies it to a private 0600 file per user; it is never embedded
  in the Claude Code config.
- **`--access-mode restricted`** — read-only, and it blocks heavy or unsafe
  queries. Use `unrestricted` only with a real write need; for inspecting the live
  swfdb, restricted is correct.

## Admin: one-time system install

`uv` and the `postgres-mcp` server are installed system-wide so opt-in users need
only the registration step. On `swf-testbed` this was done once:

```bash
# uv (static binary) into /usr/local/bin
sudo cp ~/.local/bin/uv ~/.local/bin/uvx /usr/local/bin/

# postgres-mcp as a tool whose venv + managed Python live in a world-readable /opt
sudo env UV_TOOL_DIR=/opt/uv/tools UV_TOOL_BIN_DIR=/usr/local/bin \
        UV_PYTHON_INSTALL_DIR=/opt/uv/python \
        /usr/local/bin/uv tool install postgres-mcp
sudo chmod -R a+rX /opt/uv         # so non-root users can execute it
```

The `UV_PYTHON_INSTALL_DIR` matters: without it uv places its managed Python under
root's home, where other users cannot execute it.

## Scope

This points at `swfdb` on `localhost` — the swf-testbed host, where the database is
local. PanDA and Rucio are deliberately out of scope: PanDA is read through its own
curated tools, Rucio through its API, not direct SQL.

`pg_stat_statements` is not installed on swfdb, so the workload/index-advisor tools
(`get_top_queries`, `analyze_workload_indexes`) are inert. Installing it would
require a database restart and is intentionally not done.
