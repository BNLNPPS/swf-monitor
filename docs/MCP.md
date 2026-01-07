# Model Context Protocol (MCP) Integration

## Overview

The SWF Monitor implements the [Model Context Protocol](https://modelcontextprotocol.io/) (MCP), the open standard for LLM-system interaction. This enables natural language queries and control of the testbed via MCP-compatible LLMs.

**Endpoint:** `/mcp/mcp`

**Package:** [django-mcp-server](https://github.com/omarbenhamid/django-mcp-server)

## Implementation Approach

### Architecture

MCP is integrated directly into Django rather than as a separate service. This leverages the existing infrastructure:

- **Django** serves the MCP endpoint alongside REST API
- **django-mcp-server** provides MCP spec compliance and tool registration
- **OAuth2** authentication via django-oauth-toolkit (optional, disabled by default for development)

### Tool Registration

Tools are defined in `monitor_app/mcp.py` using the `@mcp.tool()` decorator on async functions. Each function becomes an MCP tool that LLMs can discover and call.

The module is auto-discovered by django-mcp-server (must be named `mcp.py`).

Tool docstrings are critical - they are the only documentation the LLM sees when deciding which tool to use and how to call it.

### Transport

HTTP/REST transport (Streamable HTTP). The MCP spec also supports stdio and SSE transports, but HTTP aligns with the existing REST architecture.

## Client Configuration

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "swf-monitor": {
      "url": "https://pandaserver02.sdcc.bnl.gov/mcp/mcp",
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
      "url": "https://pandaserver02.sdcc.bnl.gov/mcp/mcp"
    }
  }
}
```

## Available Tools

### System Status

| Tool | Description |
|------|-------------|
| `get_system_status()` | Overall health: agent counts, running executions, recent message volume. Use first for high-level view. |

### Agents

| Tool | Description |
|------|-------------|
| `list_agents(namespace=None)` | List all agents with status and last heartbeat. Optional namespace filter. |
| `get_agent(name)` | Details for a specific agent by name. |

### Namespaces

| Tool | Description |
|------|-------------|
| `list_namespaces()` | List all testbed namespaces (isolation boundaries for workflow runs). |

### Workflows

| Tool | Description |
|------|-------------|
| `list_workflow_definitions()` | List available workflow definitions that can be executed. |
| `list_workflow_executions(namespace=None, status=None, hours=24)` | List recent executions. Filter by namespace, status, or time window. |
| `get_workflow_execution(execution_id)` | Details for a specific execution including parameters. |

### Messages

| Tool | Description |
|------|-------------|
| `list_messages(namespace=None, sender=None, message_type=None, minutes=30)` | List recent workflow messages. Filter by namespace, sender agent, or type. |

### Actions (Not Yet Implemented)

| Tool | Description |
|------|-------------|
| `start_workflow(workflow_name, namespace)` | Start a workflow execution. Returns CLI instructions for now. |
| `stop_workflow(execution_id)` | Stop a running execution. Returns status message for now. |

## Example Prompts

**System health check:**
> "What's the current status of the testbed? Are all agents running?"

The LLM calls `get_system_status()` and summarizes agent health, running workflows, and message activity.

**Investigating agents:**
> "Show me the agents in namespace torre1 and when they last sent a heartbeat"

The LLM calls `list_agents(namespace="torre1")` and presents the results.

**Workflow history:**
> "What workflows ran today and how long did they take?"

The LLM calls `list_workflow_executions(hours=24)` and calculates durations from start/end times.

**Specific lookup:**
> "Get details on execution stf_datataking-wenauseic-0042"

The LLM calls `get_workflow_execution("stf_datataking-wenauseic-0042")`.

**Cross-referencing:**
> "The fast_processing agent seems slow. Show me recent messages it sent."

The LLM calls `list_messages(sender="fast_processing_agent")` to see what the agent has been doing.

## Technical Reference

### Files

| File | Purpose |
|------|---------|
| `monitor_app/mcp.py` | Tool definitions (auto-discovered) |
| `swf_monitor_project/settings.py` | MCP and OAuth2 configuration |
| `swf_monitor_project/urls.py` | Route registration (`/mcp/`, `/o/`) |

### Settings

```python
# Django MCP Server configuration
DJANGO_MCP_GLOBAL_SERVER_CONFIG = {
    "name": "swf-monitor",
    "instructions": "ePIC Streaming Workflow Testbed monitoring and control server",
}

# OAuth2 (optional - commented out for development)
# DJANGO_MCP_AUTHENTICATION_CLASSES = [
#     "oauth2_provider.contrib.rest_framework.OAuth2Authentication",
# ]
```

### Adding New Tools

```python
# In monitor_app/mcp.py

from mcp_server import mcp_server as mcp

@mcp.tool()
async def my_new_tool(param: str) -> dict:
    """
    Tool description shown to the LLM.

    Args:
        param: Parameter description

    Returns:
        Result description
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def do_work():
        # Django ORM operations here
        return {"result": "value"}

    return await do_work()
```

### References

- [MCP Specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [django-mcp-server GitHub](https://github.com/omarbenhamid/django-mcp-server)
- [OAuth2 Provider](https://django-oauth-toolkit.readthedocs.io/)
