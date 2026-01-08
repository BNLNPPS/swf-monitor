# Model Context Protocol (MCP) Integration

## Overview

The SWF Monitor implements the [Model Context Protocol](https://modelcontextprotocol.io/) (MCP), the open standard for LLM-system interaction. This enables natural language queries and control of the testbed via MCP-compatible LLMs.

**Endpoint:** `/mcp/mcp`

**Package:** [django-mcp-server](https://github.com/omarbenhamid/django-mcp-server)

## Design Philosophy

MCP tools are **data access primitives** with filtering capabilities. The LLM synthesizes, summarizes, and aggregates information from multiple tool calls. This approach:

- Provides flexibility for unanticipated queries
- Leverages LLM reasoning capabilities
- Keeps tools simple and composable
- Supports complex analysis through multiple calls

**Date Range Convention:** All list tools support `start_time` and `end_time` parameters (ISO datetime strings). If omitted, tools default to a reasonable recent period.

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

---

## Available Tools

### Tool Discovery

| Tool | Parameters | Description |
|------|------------|-------------|
| `list_available_tools` | — | List all available MCP tools with descriptions. Use to discover capabilities. |

---

### System State

| Tool | Parameters | Description |
|------|------------|-------------|
| `get_system_state` | — | Comprehensive system state: agent counts/health, running executions, message volume, run states, persistent state. Extensible as system grows. |

**Returns:**
- `agents`: total, healthy, unhealthy counts
- `executions`: running count, recently completed
- `messages`: recent message count
- `run_states`: current fast processing run states
- `persistent_state`: system-wide persistent state
- `health`: overall system health indicator

---

### Agents

| Tool | Parameters | Description |
|------|------------|-------------|
| `list_agents` | `namespace`, `agent_type`, `status`, `execution_id`, `start_time`, `end_time` | List agents with filtering. Date range filters by heartbeat time. |
| `get_agent` | `name` (required) | Full details for a specific agent including metadata. |

**`list_agents` filters:**
- `namespace`: Filter to agents in this namespace
- `agent_type`: Filter by type (daqsim, data, processing, fastmon, etc.)
- `status`: Filter by status (OK, WARNING, ERROR)
- `execution_id`: Filter to agents that participated in this execution
- `start_time`, `end_time`: Filter by heartbeat within date range

**Returns per agent:**
- `name`, `agent_type`, `status`, `namespace`
- `last_heartbeat` (ISO timestamp)
- `workflow_enabled`, `total_stf_processed`

---

### Namespaces

| Tool | Parameters | Description |
|------|------------|-------------|
| `list_namespaces` | — | List all testbed namespaces with owners. |
| `get_namespace` | `namespace` (required), `start_time`, `end_time` | Details for a namespace including activity counts. |

**`get_namespace` returns:**
- `name`, `owner`, `description`
- `agent_count`: agents registered in namespace
- `execution_count`: workflow executions (in date range if specified)
- `message_count`: messages (in date range if specified)
- `active_users`: users who ran executions (in date range if specified)

---

### Workflow Definitions

| Tool | Parameters | Description |
|------|------------|-------------|
| `list_workflow_definitions` | `workflow_type`, `created_by` | List available workflow definitions. |

**Returns per definition:**
- `workflow_name`, `version`, `workflow_type`
- `description`, `created_by`, `created_at`
- `execution_count`: number of times executed

---

### Workflow Executions

| Tool | Parameters | Description |
|------|------------|-------------|
| `list_workflow_executions` | `namespace`, `status`, `executed_by`, `workflow_name`, `currently_running`, `start_time`, `end_time` | List workflow executions with filtering. |
| `get_workflow_execution` | `execution_id` (required) | Full details for a specific execution. |

**`list_workflow_executions` filters:**
- `namespace`: Filter to executions in this namespace
- `status`: Filter by status (pending, running, completed, failed, cancelled)
- `executed_by`: Filter by user who started the execution
- `workflow_name`: Filter by workflow definition name
- `currently_running`: If True, return all running executions (ignores date range). Use for "What's running?"
- `start_time`, `end_time`: Filter by execution start time

**Returns per execution:**
- `execution_id`, `workflow_name`, `namespace`
- `status`, `executed_by`
- `start_time`, `end_time` (ISO timestamps)
- `parameter_values`: execution configuration

---

### Messages

| Tool | Parameters | Description |
|------|------------|-------------|
| `list_messages` | `namespace`, `execution_id`, `agent`, `message_type`, `start_time`, `end_time` | List workflow messages with filtering. |

**Filters:**
- `namespace`: Filter to messages in this namespace
- `execution_id`: Filter to messages for this execution
- `agent`: Filter by sender agent name
- `message_type`: Filter by type (stf_created, processing_complete, etc.)
- `start_time`, `end_time`: Filter by sent time

**Returns per message:**
- `message_type`, `sender_agent`, `namespace`
- `sent_at` (ISO timestamp)
- `execution_id`, `run_id`
- `payload_summary`: truncated message content

---

### Runs

| Tool | Parameters | Description |
|------|------------|-------------|
| `list_runs` | `start_time`, `end_time` | List simulation runs with timing and file counts. |
| `get_run` | `run_number` (required) | Full details for a specific run. |

**`list_runs` returns per run:**
- `run_number`
- `start_time`, `end_time`, `duration`
- `stf_file_count`: number of STF files in this run

**`get_run` returns:**
- All fields above plus:
- `run_conditions`: JSON metadata
- `file_stats`: STF file counts by status (registered, processing, done, failed)

---

### STF Files

| Tool | Parameters | Description |
|------|------------|-------------|
| `list_stf_files` | `run_number`, `status`, `machine_state`, `start_time`, `end_time` | List STF files with filtering. |
| `get_stf_file` | `file_id` or `stf_filename` (required) | Full details for a specific STF file. |

**`list_stf_files` filters:**
- `run_number`: Filter to files from this run
- `status`: Filter by processing status (registered, processing, processed, done, failed)
- `machine_state`: Filter by detector state (physics, cosmics, etc.)
- `start_time`, `end_time`: Filter by creation time

**Returns per STF file:**
- `file_id`, `stf_filename`, `run_number`
- `status`, `machine_state`
- `file_size_bytes`, `created_at`
- `tf_file_count`: number of TF files derived from this STF

**`get_stf_file` returns:**
- All fields above plus:
- `checksum`, `metadata`
- `workflow_id`, `daq_state`, `daq_substate`, `workflow_status`

---

### TF Slices (Fast Processing)

| Tool | Parameters | Description |
|------|------------|-------------|
| `list_tf_slices` | `run_number`, `stf_filename`, `tf_filename`, `status`, `assigned_worker`, `start_time`, `end_time` | List TF slices for fast processing workflow. |
| `get_tf_slice` | `tf_filename`, `slice_id` (required) | Full details for a specific TF slice. |

**`list_tf_slices` filters:**
- `run_number`: Filter to slices from this run
- `stf_filename`: Filter to slices from this STF file
- `tf_filename`: Filter to slices from this TF sample
- `status`: Filter by status (queued, processing, completed, failed)
- `assigned_worker`: Filter by worker assignment
- `start_time`, `end_time`: Filter by creation time

**Returns per slice:**
- `slice_id`, `tf_filename`, `stf_filename`, `run_number`
- `tf_first`, `tf_last`, `tf_count` (TF range)
- `status`, `assigned_worker`
- `created_at`, `completed_at`

**`get_tf_slice` returns:**
- All fields above plus:
- `retries`, `assigned_at`
- `metadata`

---

### Logs

| Tool | Parameters | Description |
|------|------------|-------------|
| `list_logs` | `app_name`, `instance_name`, `level`, `search`, `start_time`, `end_time` | List log entries from all agents. |
| `get_log_entry` | `log_id` (required) | Full details for a specific log entry. |

**`list_logs` filters:**
- `app_name`: Filter by application type (e.g., 'daq_simulator', 'data_agent')
- `instance_name`: Filter by agent instance name
- `level`: Minimum level threshold - returns this level and higher severity:
  - `DEBUG` → all logs
  - `INFO` → INFO, WARNING, ERROR, CRITICAL
  - `WARNING` → WARNING, ERROR, CRITICAL
  - `ERROR` → ERROR, CRITICAL
  - `CRITICAL` → CRITICAL only
- `search`: Case-insensitive text search in message
- `start_time`, `end_time`: Filter by timestamp (default: last 24 hours)

**Returns per entry:**
- `id`, `timestamp`, `app_name`, `instance_name`
- `level`, `message`, `module`, `funcname`, `lineno`

---

### Actions (Not Yet Implemented)

| Tool | Parameters | Description |
|------|------------|-------------|
| `start_workflow` | `workflow_name`, `namespace` (required) | Start a workflow execution. Returns CLI instructions for now. |
| `stop_workflow` | `execution_id` (required) | Stop a running execution. Returns status message for now. |

---

## Tool Summary

| Category | Tools | Count |
|----------|-------|-------|
| Tool Discovery | `list_available_tools` | 1 |
| System State | `get_system_state` | 1 |
| Agents | `list_agents`, `get_agent` | 2 |
| Namespaces | `list_namespaces`, `get_namespace` | 2 |
| Workflow Definitions | `list_workflow_definitions` | 1 |
| Workflow Executions | `list_workflow_executions`, `get_workflow_execution` | 2 |
| Messages | `list_messages` | 1 |
| Runs | `list_runs`, `get_run` | 2 |
| STF Files | `list_stf_files`, `get_stf_file` | 2 |
| TF Slices | `list_tf_slices`, `get_tf_slice` | 2 |
| Logs | `list_logs`, `get_log_entry` | 2 |
| Actions | `start_workflow`, `stop_workflow` | 2 |
| **Total** | | **20** |

---

## Example Prompts

### What's Running?

> "What's running in the testbed?"

LLM calls `list_workflow_executions(currently_running=True)` and summarizes the running executions by namespace and workflow type.

> "What's the state of my running workflow?"

LLM calls `list_workflow_executions(currently_running=True, namespace="user_namespace")` or with `executed_by` filter.

### System Health

> "What's the current state of the testbed?"

LLM calls `get_system_state()` and summarizes agent health, running workflows, and system state.

### Error Discovery

> "Are there any errors in the system?"

LLM calls `list_logs(level='ERROR')` to find error and critical log entries, then summarizes the issues found.

> "Why did my workflow fail?"

LLM calls:
1. `list_workflow_executions(status='failed', namespace="user_namespace")` - find failed executions
2. `list_logs(level='ERROR', start_time="...")` - find errors around the failure time

### Activity Summary

> "Summarize testbed activity for the past week."

LLM makes multiple calls:
1. `list_workflow_executions(start_time="2026-01-01T00:00:00", end_time="2026-01-08T00:00:00")` - all executions
2. `list_agents()` - registered agents
3. `list_namespaces()` - active namespaces
4. Synthesizes: "In the past week, 47 workflow executions ran across 3 namespaces. User wenauseic ran 25 executions in namespace torre1..."

### Investigating a Run

> "Show me details about run 100042 and its STF files."

LLM calls:
1. `get_run(run_number=100042)` - run details
2. `list_stf_files(run_number=100042)` - associated STF files

### Agent Troubleshooting

> "The fast_processing agent seems slow. What has it been doing?"

LLM calls:
1. `get_agent(name="fast_processing_agent_torre1")` - agent status
2. `list_messages(agent="fast_processing_agent_torre1", start_time="2026-01-07T00:00:00")` - recent activity

### Namespace Activity

> "What's happening in namespace torre1 today?"

LLM calls:
1. `get_namespace(namespace="torre1", start_time="2026-01-08T00:00:00")` - activity counts
2. `list_workflow_executions(namespace="torre1", start_time="2026-01-08T00:00:00")` - executions
3. `list_agents(namespace="torre1")` - agents

### Fast Processing Status

> "What's the status of TF slice processing for run 100042?"

LLM calls:
1. `list_tf_slices(run_number=100042)` - all slices
2. Summarizes by status: "Run 100042 has 150 slices: 120 completed, 25 processing, 5 queued."

---

## Technical Reference

### File Locations

The MCP service spans multiple files in the `swf-monitor` repository:

```
swf-monitor/
├── docs/
│   └── MCP.md                              # This documentation
├── src/
│   ├── monitor_app/
│   │   └── mcp.py                          # Tool definitions (API code)
│   └── swf_monitor_project/
│       ├── settings.py                     # Server config & instructions
│       └── urls.py                         # Route registration (/mcp/)
```

| File | Purpose |
|------|---------|
| `src/monitor_app/mcp.py` | **Tool definitions** - all MCP tool functions with docstrings |
| `src/swf_monitor_project/settings.py` | **Server config** - `DJANGO_MCP_GLOBAL_SERVER_CONFIG` with name and instructions |
| `src/swf_monitor_project/urls.py` | **Route registration** - mounts MCP at `/mcp/` |
| `docs/MCP.md` | **Documentation** - this file |

### Architecture

MCP is integrated directly into Django rather than as a separate service:

- **Django** serves the MCP endpoint alongside REST API
- **django-mcp-server** provides MCP spec compliance and tool registration
- **OAuth2** authentication via django-oauth-toolkit (optional, disabled for development)

### Tool Registration

Tools are defined in `monitor_app/mcp.py` using the `@mcp.tool()` decorator on async functions. Each function becomes an MCP tool that LLMs can discover and call.

The module is auto-discovered by django-mcp-server (must be named `mcp.py`).

**Tool docstrings are critical** - they are the only documentation the LLM sees when deciding which tool to use and how to call it.

### Transport

HTTP/REST transport (Streamable HTTP). The MCP spec also supports stdio and SSE transports, but HTTP aligns with the existing REST architecture.

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
async def my_new_tool(param: str, start_time: str = None, end_time: str = None) -> dict:
    """
    Tool description shown to the LLM.

    Args:
        param: Parameter description
        start_time: Optional ISO datetime for range start
        end_time: Optional ISO datetime for range end

    Returns:
        Result description
    """
    from asgiref.sync import sync_to_async
    from django.utils.dateparse import parse_datetime

    @sync_to_async
    def do_work():
        queryset = MyModel.objects.all()

        # Apply date range filter
        if start_time:
            queryset = queryset.filter(created_at__gte=parse_datetime(start_time))
        if end_time:
            queryset = queryset.filter(created_at__lte=parse_datetime(end_time))

        return [{"field": obj.field} for obj in queryset]

    return await do_work()
```

### References

- [MCP Specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [django-mcp-server GitHub](https://github.com/omarbenhamid/django-mcp-server)
- [OAuth2 Provider](https://django-oauth-toolkit.readthedocs.io/)
