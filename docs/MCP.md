# Model Context Protocol (MCP) Integration

## Overview

The SWF Monitor implements the [Model Context Protocol](https://modelcontextprotocol.io/) (MCP), the open standard for LLM-system interaction. This enables natural language queries and control of the testbed via MCP-compatible LLMs.

**Endpoint:** `/swf-monitor/mcp/`

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
      "url": "https://pandaserver02.sdcc.bnl.gov/swf-monitor/mcp/",
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
      "url": "https://pandaserver02.sdcc.bnl.gov/swf-monitor/mcp/"
    }
  }
}
```

## Authentication

The MCP endpoint supports two authentication modes:

### Claude Code (Local)

POST requests (used by Claude Code) pass through without authentication. This enables local development and CLI-based MCP access without OAuth setup.

### Claude.ai (Remote)

GET requests require OAuth 2.1 Bearer token authentication via Auth0. This enables Claude.ai remote MCP connections with proper authorization.

**OAuth Flow:**
1. Claude.ai discovers OAuth metadata via `/.well-known/oauth-protected-resource`
2. User authenticates with Auth0
3. Claude.ai includes Bearer token in requests
4. MCP middleware validates JWT against Auth0 JWKS

**Configuration (production):**
```bash
# In .env or environment
AUTH0_DOMAIN=your-tenant.us.auth0.com
AUTH0_CLIENT_ID=your-client-id
AUTH0_CLIENT_SECRET=your-client-secret
AUTH0_API_IDENTIFIER=https://your-server/swf-monitor/mcp
```

Leave `AUTH0_DOMAIN` empty to disable OAuth (allows all requests through).

**Network Requirements:**
Claude.ai connects from Anthropic's servers, so the MCP endpoint must be accessible from the public internet. Internal networks (e.g., behind lab firewalls) may require network configuration to allow external access.

---

### Claude Code Settings Example

Full `~/.claude/settings.json` with swf-monitor MCP server, permissions, and status line:

```json
{
  "mcpServers": {
    "swf-monitor": {
      "type": "http",
      "url": "https://pandaserver02.sdcc.bnl.gov/swf-monitor/mcp/"
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

## Available Tools

### Tool Discovery

| Tool | Parameters | Description |
|------|------------|-------------|
| `swf_list_available_tools` | - | List all available MCP tools with descriptions. Use to discover capabilities. |

---

### System State

| Tool | Parameters | Description |
|------|------------|-------------|
| `swf_get_system_state` | `username` | Comprehensive system state for a user: context from testbed.toml, agent manager status, workflow runner readiness, agent counts, execution stats. |

**Parameters:**
- `username`: Optional. Username to get context for (reads their testbed.toml). If not provided, infers from SWF_HOME environment variable.

**Returns:**
- `timestamp`: Current server time
- `user_context`: namespace, workflow defaults from user's testbed.toml
- `agent_manager`: Status of user's agent manager daemon (healthy/unhealthy/missing/exited)
- `workflow_runner`: Status of healthy DAQ_Simulator that can accept swf_start_workflow
- `ready_to_run`: Boolean - True if workflow_runner is healthy and can accept commands
- `last_execution`: Most recent workflow execution for user's namespace
- `errors_last_hour`: Count of ERROR logs in user's namespace
- `agents`: Total, active, exited, healthy, unhealthy counts
- `executions`: Running count, completed in last hour
- `messages_last_10min`: Recent message count
- `run_states`: Current fast processing run states
- `persistent_state`: System-wide persistent state (next IDs, etc.)
- `recent_events`: Last 10 system state events

---

### Agents

| Tool | Parameters | Description |
|------|------------|-------------|
| `swf_list_agents` | `namespace`, `agent_type`, `status`, `execution_id`, `start_time`, `end_time` | List agents with filtering. **Excludes EXITED agents by default.** |
| `swf_get_agent` | `name` (required) | Full details for a specific agent including metadata. |

**`swf_list_agents` filters:**
- `namespace`: Filter to agents in this namespace
- `agent_type`: Filter by type (daqsim, data, processing, fastmon, workflow_runner, etc.)
- `status`: Filter by status. Special values:
  - `None` (default): Excludes EXITED agents
  - `'EXITED'`: Show only exited agents
  - `'all'`: Show all agents regardless of status
  - `'OK'`, `'WARNING'`, `'ERROR'`: Filter to specific status
- `execution_id`: Filter to agents that participated in this execution
- `start_time`, `end_time`: Filter by heartbeat within date range

**Returns per agent:**
- `name`, `agent_type`, `status`, `operational_state`, `namespace`
- `last_heartbeat` (ISO timestamp)
- `workflow_enabled`, `total_stf_processed`

---

### Namespaces

| Tool | Parameters | Description |
|------|------------|-------------|
| `swf_list_namespaces` | - | List all testbed namespaces with owners. |
| `swf_get_namespace` | `namespace` (required), `start_time`, `end_time` | Details for a namespace including activity counts. |

**`swf_get_namespace` returns:**
- `name`, `owner`, `description`
- `agent_count`: Agents registered in namespace
- `execution_count`: Workflow executions (in date range if specified)
- `message_count`: Messages (in date range if specified)
- `active_users`: Users who ran executions (in date range if specified)

---

### Workflow Definitions

| Tool | Parameters | Description |
|------|------------|-------------|
| `swf_list_workflow_definitions` | `workflow_type`, `created_by` | List available workflow definitions. |

**Returns per definition:**
- `workflow_name`, `version`, `workflow_type`
- `description`, `created_by`, `created_at`
- `execution_count`: Number of times executed

---

### Workflow Executions

| Tool | Parameters | Description |
|------|------------|-------------|
| `swf_list_workflow_executions` | `namespace`, `status`, `executed_by`, `workflow_name`, `currently_running`, `start_time`, `end_time` | List workflow executions with filtering. |
| `swf_get_workflow_execution` | `execution_id` (required) | Full details for a specific execution. |

**`swf_list_workflow_executions` filters:**
- `namespace`: Filter to executions in this namespace
- `status`: Filter by status (pending, running, completed, failed, terminated)
- `executed_by`: Filter by user who started the execution
- `workflow_name`: Filter by workflow definition name
- `currently_running`: If True, return all running executions (ignores date range). Use for "What's running?"
- `start_time`, `end_time`: Filter by execution start time

**Returns per execution:**
- `execution_id`, `workflow_name`, `namespace`
- `status`, `executed_by`
- `start_time`, `end_time` (ISO timestamps)
- `parameter_values`: Execution configuration

---

### Messages

| Tool | Parameters | Description |
|------|------------|-------------|
| `swf_list_messages` | `namespace`, `execution_id`, `agent`, `message_type`, `start_time`, `end_time` | List workflow messages with filtering. |
| `swf_send_message` | `message` (required), `message_type`, `metadata` | Send a message to the monitoring stream. |

**Diagnostic use cases:**
- Track workflow progress: `swf_list_messages(execution_id='stf_datataking-user-0044')`
- See what an agent sent: `swf_list_messages(agent='daq_simulator-agent-user-123')`
- Debug message flow: `swf_list_messages(namespace='torre1', start_time='2026-01-13T11:00:00')`
- For workflow failures: use `swf_list_logs(level='ERROR')` instead

**Common message types:** `run_imminent`, `start_run`, `stf_gen`, `end_run`, `pause_run`, `resume_run`

**Filters:**
- `namespace`: Filter to messages in this namespace
- `execution_id`: Filter to messages for this execution
- `agent`: Filter by sender agent name
- `message_type`: Filter by type (stf_gen, start_run, etc.)
- `start_time`, `end_time`: Filter by sent time (default: last 1 hour)

**Returns per message (max 200):**
- `message_type`, `sender_agent`, `namespace`
- `sent_at` (ISO timestamp)
- `execution_id`, `run_id`
- `payload_summary`: Truncated message content

**`swf_send_message` parameters:**
- `message` (required): The message text to send
- `message_type`: Type of message (default: 'announcement')
  - `'test'`: Namespace is omitted (for pipeline testing)
  - `'announcement'`, `'status'`, etc.: Uses configured namespace from testbed.toml
- `metadata`: Optional dict of additional key-value data

**`swf_send_message` behavior:**
- Sender is automatically identified as `{username}-personal-agent`
- Messages are sent to `/topic/epictopic` and captured by the monitor
- Use for: testing the message pipeline, announcements to colleagues, or any broadcast purpose

**Returns:**
- `success`: Whether the message was sent
- `sender`: The sender identifier (e.g., 'wenauseic-personal-agent')
- `message_type`: The type of message sent
- `namespace`: The namespace used (or null for test messages)
- `content`: The message content

---

### Runs

| Tool | Parameters | Description |
|------|------------|-------------|
| `swf_list_runs` | `start_time`, `end_time` | List simulation runs with timing and file counts. |
| `swf_get_run` | `run_number` (required) | Full details for a specific run. |

**`swf_list_runs` returns per run:**
- `run_number`
- `start_time`, `end_time`, `duration_seconds`
- `stf_file_count`: Number of STF files in this run

**`swf_get_run` returns:**
- All fields above plus:
- `run_conditions`: JSON metadata
- `file_stats`: STF file counts by status (registered, processing, done, failed)

---

### STF Files

| Tool | Parameters | Description |
|------|------------|-------------|
| `swf_list_stf_files` | `run_number`, `status`, `machine_state`, `start_time`, `end_time` | List STF files with filtering. |
| `swf_get_stf_file` | `file_id` or `stf_filename` (one required) | Full details for a specific STF file. |

**`swf_list_stf_files` filters:**
- `run_number`: Filter to files from this run
- `status`: Filter by processing status (registered, processing, processed, done, failed)
- `machine_state`: Filter by detector state (physics, cosmics, etc.)
- `start_time`, `end_time`: Filter by creation time

**Returns per STF file:**
- `file_id`, `stf_filename`, `run_number`
- `status`, `machine_state`
- `file_size_bytes`, `created_at`
- `tf_file_count`: Number of TF files derived from this STF

**`swf_get_stf_file` returns:**
- All fields above plus:
- `checksum`, `metadata`
- `workflow_id`, `daq_state`, `daq_substate`, `workflow_status`

---

### TF Slices (Fast Processing)

| Tool | Parameters | Description |
|------|------------|-------------|
| `swf_list_tf_slices` | `run_number`, `stf_filename`, `tf_filename`, `status`, `assigned_worker`, `start_time`, `end_time` | List TF slices for fast processing workflow. |
| `swf_get_tf_slice` | `tf_filename`, `slice_id` (both required) | Full details for a specific TF slice. |

**`swf_list_tf_slices` filters:**
- `run_number`: Filter to slices from this run
- `stf_filename`: Filter to slices from this STF file
- `tf_filename`: Filter to slices from this TF sample
- `status`: Filter by status (queued, processing, completed, failed)
- `assigned_worker`: Filter by worker assignment
- `start_time`, `end_time`: Filter by creation time

**Returns per slice (max 200):**
- `slice_id`, `tf_filename`, `stf_filename`, `run_number`
- `tf_first`, `tf_last`, `tf_count` (TF range)
- `status`, `assigned_worker`
- `created_at`, `completed_at`

**`swf_get_tf_slice` returns:**
- All fields above plus:
- `retries`, `assigned_at`
- `metadata`

---

### Logs

| Tool | Parameters | Description |
|------|------------|-------------|
| `swf_list_logs` | `app_name`, `instance_name`, `execution_id`, `level`, `search`, `start_time`, `end_time` | List log entries from all agents. |
| `swf_get_log_entry` | `log_id` (required) | Full details for a specific log entry. |

**Diagnostic use cases:**
- Workflow logs: `swf_list_logs(execution_id='stf_datataking-user-0044')`
- Debug a specific agent: `swf_list_logs(instance_name='daq_simulator-agent-user-123')`
- Find all errors: `swf_list_logs(level='ERROR')`
- Search for specific issues: `swf_list_logs(search='connection failed')`

**`swf_list_logs` filters:**
- `app_name`: Filter by application type (e.g., 'daq_simulator', 'data_agent')
- `instance_name`: Filter by agent instance name
- `execution_id`: Filter by workflow execution ID (e.g., 'stf_datataking-wenauseic-0044')
- `level`: Minimum level threshold - returns this level and higher severity:
  - `DEBUG` -> all logs
  - `INFO` -> INFO, WARNING, ERROR, CRITICAL
  - `WARNING` -> WARNING, ERROR, CRITICAL
  - `ERROR` -> ERROR, CRITICAL
  - `CRITICAL` -> CRITICAL only
- `search`: Case-insensitive text search in message
- `start_time`, `end_time`: Filter by timestamp (default: last 24 hours)

**Returns per entry (max 200):**
- `id`, `timestamp`, `app_name`, `instance_name`
- `level`, `message`, `module`, `funcname`, `lineno`
- `extra_data`: Additional context (execution_id, run_id, etc.)

---

### Workflow Control

| Tool | Parameters | Description |
|------|------------|-------------|
| `swf_start_workflow` | `workflow_name`, `namespace`, `config`, `realtime`, `duration`, `stf_count`, `physics_period_count`, `physics_period_duration`, `stf_interval` | Start a workflow by sending command to DAQ Simulator agent. |
| `swf_stop_workflow` | `execution_id` (required) | Stop a running workflow gracefully. |
| `swf_end_execution` | `execution_id` (required) | Mark a stuck execution as terminated (database state change only). |

**`swf_start_workflow` parameters:**

All parameters are optional - defaults are read from the user's `testbed.toml`:
- `workflow_name`: Name of workflow (default: from config, typically 'stf_datataking')
- `namespace`: Testbed namespace (default: from config)
- `config`: Workflow config name (default: from config, e.g., 'fast_processing_default')
- `realtime`: Run in real-time mode (default: from config, typically True)
- `duration`: Max duration in seconds (0 = run until complete)
- `stf_count`: Number of STF files to generate (overrides config)
- `physics_period_count`: Number of physics periods (overrides config)
- `physics_period_duration`: Duration of each physics period in seconds (overrides config)
- `stf_interval`: Interval between STF generation in seconds (overrides config)

**Returns:** Success/failure status with execution details. Workflow runs asynchronously.

**After starting, monitor with:**
- `get_workflow_execution(execution_id)` -> status: running/completed/failed/terminated
- `swf_list_messages(execution_id='...')` -> progress events
- `swf_list_logs(execution_id='...')` -> workflow logs including errors
- `get_workflow_monitor(execution_id)` -> aggregated status and events

**`swf_stop_workflow`:** Sends a stop command to the DAQ Simulator agent. The workflow stops gracefully at the next checkpoint. Use `swf_list_workflow_executions(currently_running=True)` to find running execution IDs.

**`swf_end_execution`:** Use to clean up stale or stuck executions that are still marked as 'running' in the database. This is a state change only - no agent message is sent.

---

### Agent Process Management

| Tool | Parameters | Description |
|------|------------|-------------|
| `swf_kill_agent` | `name` (required) | Kill an agent process by sending SIGKILL to its PID. |

**`swf_kill_agent` behavior:**
- Looks up the agent by `instance_name`
- Retrieves its `pid` and `hostname`
- Sends SIGKILL if the agent is on the current host
- Always marks the agent's status and operational_state as `EXITED`
- Agent will no longer appear in default `swf_list_agents` results

**Returns:**
- `success`: Whether the operation completed
- `killed`: Whether the process was actually killed (may be False if already dead or on different host)
- `kill_error`: Error message if kill failed (permission denied, process not found, remote host)
- `old_state`, `new_state`: State transition

---

### User Agent Manager

The User Agent Manager is a per-user daemon that enables MCP-driven testbed control. It listens for commands on a user-specific queue and manages supervisord-controlled agents.

| Tool | Parameters | Description |
|------|------------|-------------|
| `swf_check_agent_manager` | `username` | Check if a user's agent manager daemon is alive. |
| `swf_start_user_testbed` | `username`, `config_name` | Start a user's testbed via their agent manager. |
| `swf_stop_user_testbed` | `username` | Stop a user's testbed via their agent manager. |

**`swf_check_agent_manager` returns:**
- `alive`: True if agent manager has recent heartbeat (within 5 minutes)
- `username`: The user being checked
- `instance_name`: The agent manager's instance name (e.g., 'agent-manager-wenauseic')
- `last_heartbeat`: When it last checked in
- `operational_state`: Current state (READY, EXITED, etc.)
- `control_queue`: The queue to send commands to (e.g., '/queue/agent_control.wenauseic')
- `agents_running`: Whether testbed agents are currently running
- `how_to_start`: Instructions if not alive

**`swf_start_user_testbed`:**
- Sends `start_testbed` command to the user's agent manager
- Agent manager must be running first (use `swf_check_agent_manager` to verify)
- `config_name`: Optional config name (e.g., 'fast_processing'). Uses default if not specified.
- Agents start asynchronously - use `swf_list_agents` to verify

**`swf_stop_user_testbed`:**
- Sends `stop_testbed` command to the user's agent manager
- If agent manager is not running, use `swf_kill_agent` to stop agents directly

**Starting the agent manager:**
```bash
cd /data/<username>/github/swf-testbed
source .venv/bin/activate && source ~/.env
testbed agent-manager
```

---

### Workflow Monitoring

| Tool | Parameters | Description |
|------|------------|-------------|
| `swf_get_workflow_monitor` | `execution_id` (required) | Get aggregated status and events for a workflow execution. |
| `swf_list_workflow_monitors` | - | List recent executions that can be monitored. |

**`swf_get_workflow_monitor` returns:**
- `execution_id`: The execution being monitored
- `status`: Current workflow status (running/completed/failed/terminated)
- `phase`: Current phase (imminent/running/ended/unknown)
- `run_id`: The run number for this execution
- `stf_count`: Number of STF files generated
- `events`: List of key events with timestamps (run_imminent, start_run, end_run)
- `errors`: List of any errors encountered (from messages and logs)
- `start_time`, `end_time`: Execution timestamps
- `duration_seconds`: How long the workflow ran (if completed)

This tool aggregates information from workflow messages and logs, providing a single-call summary of workflow progress without needing to poll multiple tools.

**`swf_list_workflow_monitors` returns:**
- List of executions from last 24 hours with: `execution_id`, `status`, `start_time`, `end_time`, `stf_count`
- Use to pick an execution for detailed monitoring with `swf_get_workflow_monitor`

---

## Tool Summary

| Category | Tools | Count |
|----------|-------|-------|
| Tool Discovery | `swf_list_available_tools` | 1 |
| System State | `swf_get_system_state` | 1 |
| Agents | `swf_list_agents`, `swf_get_agent` | 2 |
| Namespaces | `swf_list_namespaces`, `swf_get_namespace` | 2 |
| Workflow Definitions | `swf_list_workflow_definitions` | 1 |
| Workflow Executions | `swf_list_workflow_executions`, `swf_get_workflow_execution` | 2 |
| Messages | `swf_list_messages`, `swf_send_message` | 2 |
| Runs | `swf_list_runs`, `swf_get_run` | 2 |
| STF Files | `swf_list_stf_files`, `swf_get_stf_file` | 2 |
| TF Slices | `swf_list_tf_slices`, `swf_get_tf_slice` | 2 |
| Logs | `swf_list_logs`, `swf_get_log_entry` | 2 |
| Workflow Control | `swf_start_workflow`, `swf_stop_workflow`, `swf_end_execution` | 3 |
| Agent Management | `swf_kill_agent` | 1 |
| User Agent Manager | `swf_check_agent_manager`, `swf_start_user_testbed`, `swf_stop_user_testbed` | 3 |
| Workflow Monitoring | `swf_get_workflow_monitor`, `swf_list_workflow_monitors` | 2 |
| **Total** | | **28** |

---

## Quick Reference - Example Prompts

          System Readiness
          - "What's the state of the testbed?"
          - "Am I ready to run a workflow?"
          - "Is my agent manager running?"
          - "Are there any errors in the system?"

          Starting the Testbed
          - "Start my testbed"
          - "Start my testbed with the fast_processing config"
          - "Check if my agents are running"

          Running Workflows
          - "Start a workflow"
          - "Run a workflow with 5 STF files"
          - "Start a workflow with 3 physics periods"
          - "What's running right now?"

          Monitoring
          - "What's the status of my workflow?"
          - "Show me the progress of execution stf_datataking-wenauseic-0045"
          - "How many STF files have been generated?"
          - "Are there any errors in my workflow?"

          Stopping
          - "Stop my running workflow"
          - "Stop the testbed"

          Troubleshooting
          - "Why did my workflow fail?"
          - "Show me the logs for the DAQ simulator"
          - "What errors happened in the last hour?"
          - "Kill the stuck daq_simulator agent"

          Combined Operations
          - "Start my testbed and run a workflow with 10 STF files"
          - "Check if I'm ready to run, and if so, start a workflow"

---

## Example Prompts - Detailed

### What's Running?

> "What's running in the testbed?"

LLM calls `swf_list_workflow_executions(currently_running=True)` and summarizes the running executions by namespace and workflow type.

> "What's the state of my running workflow?"

LLM calls `get_workflow_monitor(execution_id='...')` for aggregated status, or `swf_list_workflow_executions(currently_running=True, namespace="user_namespace")`.

### System Health

> "What's the current state of the testbed?"

LLM calls `swf_get_system_state(username='wenauseic')` and summarizes user context, agent health, running workflows, and system state.

> "Am I ready to run a workflow?"

LLM calls `swf_get_system_state(username='...')` and checks `ready_to_run` field. If False, explains what's missing (agent manager, workflow runner).

### Starting and Stopping Workflows

> "Start a workflow with 5 STF files"

LLM calls `swf_start_workflow(stf_count=5)` - other parameters default from testbed.toml.

> "Stop my running workflow"

LLM calls `swf_list_workflow_executions(currently_running=True)` to find the execution_id, then `swf_stop_workflow(execution_id='...')`.

### Error Discovery

> "Are there any errors in the system?"

LLM calls `swf_list_logs(level='ERROR')` to find error and critical log entries, then summarizes the issues found.

> "Why did my workflow fail?"

LLM calls:
1. `swf_list_workflow_executions(status='failed', namespace="user_namespace")` - find failed executions
2. `get_workflow_monitor(execution_id='...')` - get aggregated errors
3. `swf_list_logs(execution_id='...', level='ERROR')` - detailed error logs

### Activity Summary

> "Summarize testbed activity for the past week."

LLM makes multiple calls:
1. `swf_list_workflow_executions(start_time="2026-01-06T00:00:00", end_time="2026-01-13T00:00:00")` - all executions
2. `swf_list_agents()` - registered agents
3. `swf_list_namespaces()` - active namespaces
4. Synthesizes: "In the past week, 47 workflow executions ran across 3 namespaces..."

### Investigating a Run

> "Show me details about run 100042 and its STF files."

LLM calls:
1. `swf_get_run(run_number=100042)` - run details
2. `list_stf_files(run_number=100042)` - associated STF files

### Agent Troubleshooting

> "The fast_processing agent seems unresponsive. What's happening?"

LLM calls:
1. `swf_get_agent(name="fast_processing-agent-wenauseic-123")` - agent status
2. `swf_list_logs(instance_name="fast_processing-agent-wenauseic-123", level='WARNING')` - recent issues
3. If needed: `kill_agent(name="...")` to terminate unresponsive agent

### Managing User Testbed

> "Start my testbed"

LLM calls:
1. `check_agent_manager(username='wenauseic')` - verify agent manager is running
2. If alive: `start_user_testbed(username='wenauseic')`
3. If not: Instructs user to run `testbed agent-manager`

### Namespace Activity

> "What's happening in namespace torre1 today?"

LLM calls:
1. `swf_get_namespace(namespace="torre1", start_time="2026-01-13T00:00:00")` - activity counts
2. `swf_list_workflow_executions(namespace="torre1", start_time="2026-01-13T00:00:00")` - executions
3. `swf_list_agents(namespace="torre1")` - agents

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
│   │   ├── mcp.py                          # Tool definitions (API code)
│   │   ├── auth0.py                        # JWT validation with Auth0 JWKS
│   │   ├── middleware.py                   # MCPAuthMiddleware for OAuth
│   │   └── views.py                        # OAuth protected resource metadata
│   └── swf_monitor_project/
│       ├── settings.py                     # Server config, Auth0 settings
│       └── urls.py                         # Route registration (/mcp/, /.well-known/)
```

| File | Purpose |
|------|---------|
| `src/monitor_app/mcp.py` | **Tool definitions** - all MCP tool functions with docstrings |
| `src/monitor_app/auth0.py` | **Auth0 integration** - JWT validation, JWKS caching |
| `src/monitor_app/middleware.py` | **Authentication middleware** - MCPAuthMiddleware for OAuth 2.1 |
| `src/swf_monitor_project/settings.py` | **Server config** - MCP config, Auth0 settings |
| `src/swf_monitor_project/urls.py` | **Route registration** - mounts MCP at `/mcp/`, OAuth metadata at `/.well-known/` |
| `docs/MCP.md` | **Documentation** - this file |

### Architecture

MCP is integrated directly into Django rather than as a separate service:

- **Django** serves the MCP endpoint alongside REST API
- **django-mcp-server** provides MCP spec compliance and tool registration
- **Auth0 OAuth 2.1** authentication for Claude.ai remote connections (optional, disabled if AUTH0_DOMAIN not set)

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

# Auth0 OAuth 2.1 configuration (optional - leave AUTH0_DOMAIN empty to disable)
AUTH0_DOMAIN = config("AUTH0_DOMAIN", default="")
AUTH0_CLIENT_ID = config("AUTH0_CLIENT_ID", default="")
AUTH0_CLIENT_SECRET = config("AUTH0_CLIENT_SECRET", default="")
AUTH0_API_IDENTIFIER = config("AUTH0_API_IDENTIFIER", default="")
AUTH0_ALGORITHMS = ["RS256"]
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

**IMPORTANT:** After adding a new `@mcp.tool()`, you MUST also:
1. Add the tool to the hardcoded list in `list_available_tools()` at the top of `mcp.py`
2. Update the server instructions in `settings.py` (`DJANGO_MCP_GLOBAL_SERVER_CONFIG`)
3. Update this documentation (`docs/MCP.md`)

The `list_available_tools()` hardcoded list is what LLMs see when discovering available tools - if your tool isn't in that list, LLMs won't know it exists.

### References

- [MCP Specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [django-mcp-server GitHub](https://github.com/omarbenhamid/django-mcp-server)
- [OAuth2 Provider](https://django-oauth-toolkit.readthedocs.io/)
