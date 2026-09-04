# MCP Client Setup

This document covers client configuration for the SWF Monitor MCP server. The
main integration overview is [MCP.md](MCP.md), and the full tool catalog is
[MCP_TOOL_REFERENCE.md](MCP_TOOL_REFERENCE.md).

## Client Configuration

### Claude Desktop

For clients running on swf-testbed, add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "swf-monitor": {
      "url": "http://127.0.0.1:8001/swf-monitor/mcp/",
      "transport": "http"
    }
  }
}
```

### Claude Code

Add via `/mcp add` or create `.mcp.json` in project:

```json
{
  "mcpServers": {
    "swf-monitor": {
      "type": "http",
      "url": "http://127.0.0.1:8001/swf-monitor/mcp/"
    }
  }
}
```

## External access through devcloud

Outside BNL the same toolset is reached through swf-remote at
`https://epic-devcloud.org/prod/mcp/`, which proxies the monitor's
endpoint and identifies the caller (EXTERNAL_ACCESS.md, MCP through the
proxy). Opening that URL in a browser shows the self-contained setup
page for people and their assistants; the System menu's MCP item leads
there on the external face and to this endpoint's own setup page on the
internal face. Sign in at https://epic-devcloud.org/prod/ , open
https://epic-devcloud.org/prod/account/tokens/ , create a token with a
label and copy it once; the same page revokes it. Then:

```bash
claude mcp add --transport http swf-devcloud https://epic-devcloud.org/prod/mcp/ \
  --header "Authorization: Bearer <token>"
```

The token is swf-remote's and never reaches the monitor; tools run as
the signed-in user, and a tool that takes a `username` fills it in.

## Authentication

The operational MCP clients on swf-testbed are local clients: Claude Code,
the PanDA Mattermost bot, the testbed bot, scripts, and the watchdog. They use
the loopback ASGI endpoint and do not require OAuth.

The public Apache path still exists, but remote Claude.ai GET/SSE streaming is
not an operational dependency and should not be treated as supported by the
current deployment. If remote MCP access is reintroduced, require OAuth 2.1
and revalidate the transport lifecycle under load before enabling it.

**Configuration (production):**
```bash
# In .env or environment
AUTH0_DOMAIN=your-tenant.us.auth0.com
AUTH0_CLIENT_ID=your-client-id
AUTH0_CLIENT_SECRET=your-client-secret
AUTH0_API_IDENTIFIER=https://your-server/swf-monitor/mcp
```

Leave `AUTH0_DOMAIN` empty to disable OAuth. Do not expose unauthenticated
remote MCP access.

---

### Claude Code Settings Example

Full `~/.claude/settings.json` with swf-monitor MCP server, permissions, and status line:

```json
{
  "mcpServers": {
    "swf-monitor": {
      "type": "http",
      "url": "http://127.0.0.1:8001/swf-monitor/mcp/"
    }
  },
  "statusLine": {
    "type": "command",
    "command": "~/.claude/statusline.sh"
  },
  "permissions": {
    "allow": [
      "Bash(ls:*)",
      "Bash(wc:*)",
      "Bash(grep:*)",
      "mcp__swf-monitor__get_server_instructions",
      "mcp__swf-monitor__swf_list_agents",
      "mcp__swf-monitor__swf_get_agent",
      "mcp__swf-monitor__swf_list_workflow_executions",
      "mcp__swf-monitor__swf_get_workflow_execution",
      "mcp__swf-monitor__swf_list_logs",
      "mcp__swf-monitor__swf_get_system_state",
      "WebSearch",
      "WebFetch"
    ],
    "defaultMode": "default"
  },
  "alwaysThinkingEnabled": true
}
```

**Status line script** (`~/.claude/statusline.sh`):

```bash
#!/bin/bash
input=$(cat)
MODEL=$(echo "$input" | jq -r '.model.display_name')
USED=$(echo "$input" | jq -r '.context_window.used_percentage // 0')
REMAINING=$(echo "$input" | jq -r '.context_window.remaining_percentage // 100')
echo "[$MODEL] ${USED}% used | ${REMAINING}% remaining"
```

---
