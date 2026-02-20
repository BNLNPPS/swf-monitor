"""
Common utilities and tool discovery for MCP tools.
"""

import logging
from datetime import timedelta
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)


def _parse_time(time_str):
    """Parse ISO datetime string, return None if invalid."""
    if not time_str:
        return None
    try:
        return parse_datetime(time_str)
    except (ValueError, TypeError):
        return None


def _default_start_time(hours=24):
    """Return default start time (N hours ago)."""
    return timezone.now() - timedelta(hours=hours)


def _get_username(username: str = None) -> str:
    """Validate and return the username. Must be provided by the caller."""
    if not username:
        raise RuntimeError("username parameter is required")
    return username


def _monitor_url(path: str) -> str:
    """Build a full monitor URL from a path."""
    import os
    base = os.getenv('SWF_MONITOR_HTTP_URL', 'http://localhost:8000/swf-monitor')
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _get_testbed_config_path() -> tuple:
    """
    Get the testbed config file path.

    Returns:
        (Path, str): Tuple of (config_path, source) where source is
                     'SWF_TESTBED_CONFIG' or 'default'
    """
    import os
    from pathlib import Path

    swf_home = os.getenv('SWF_HOME', '')
    config_env = os.getenv('SWF_TESTBED_CONFIG')

    if config_env:
        # Env var can be absolute or relative to workflows/
        if os.path.isabs(config_env):
            return Path(config_env), 'SWF_TESTBED_CONFIG'
        else:
            return Path(swf_home) / 'swf-testbed' / 'workflows' / config_env, 'SWF_TESTBED_CONFIG'
    else:
        return Path(swf_home) / 'swf-testbed' / 'workflows' / 'testbed.toml', 'default'


# -----------------------------------------------------------------------------
# Tool Discovery
# -----------------------------------------------------------------------------
# IMPORTANT: When adding a new @mcp.tool(), you MUST also add it to the
# hardcoded list below. This list is what LLMs see when discovering tools.
#
# Also update: server instructions in settings.py, docs/MCP.md
# -----------------------------------------------------------------------------

def get_available_tools_list() -> list:
    """
    Return the hardcoded list of all available MCP tools.
    Called by swf_list_available_tools in __init__.py.
    """
    return [
        {
            "name": "swf_list_available_tools",
            "description": "List all available MCP tools with descriptions",
            "parameters": [],
        },
        {
            "name": "swf_get_system_state",
            "description": "Get comprehensive system state: user context, agent manager, workflow runner, readiness, agents, executions",
            "parameters": ["username"],
        },
        {
            "name": "swf_list_agents",
            "description": "List registered agents. Excludes EXITED agents by default. Use status='EXITED' to see exited, status='all' to see all.",
            "parameters": ["namespace", "agent_type", "status", "execution_id", "start_time", "end_time"],
        },
        {
            "name": "swf_get_agent",
            "description": "Get detailed information about a specific agent",
            "parameters": ["name"],
        },
        {
            "name": "swf_list_namespaces",
            "description": "List all testbed namespaces (isolation boundaries for users)",
            "parameters": [],
        },
        {
            "name": "swf_get_namespace",
            "description": "Get namespace details including activity counts",
            "parameters": ["namespace", "start_time", "end_time"],
        },
        {
            "name": "swf_list_workflow_definitions",
            "description": "List available workflow definitions that can be executed",
            "parameters": ["workflow_type", "created_by"],
        },
        {
            "name": "swf_list_workflow_executions",
            "description": "List workflow executions. Use currently_running=True to see what's running now",
            "parameters": ["namespace", "status", "executed_by", "workflow_name", "currently_running", "start_time", "end_time"],
        },
        {
            "name": "swf_get_workflow_execution",
            "description": "Get detailed information about a specific workflow execution",
            "parameters": ["execution_id"],
        },
        {
            "name": "swf_list_messages",
            "description": "List workflow messages between agents for debugging",
            "parameters": ["namespace", "execution_id", "agent", "message_type", "start_time", "end_time"],
        },
        {
            "name": "swf_list_runs",
            "description": "List simulation runs with timing and STF file counts",
            "parameters": ["start_time", "end_time"],
        },
        {
            "name": "swf_get_run",
            "description": "Get detailed information about a specific run",
            "parameters": ["run_number"],
        },
        {
            "name": "swf_list_stf_files",
            "description": "List STF (Super Time Frame) files with filtering",
            "parameters": ["run_number", "status", "machine_state", "start_time", "end_time"],
        },
        {
            "name": "swf_get_stf_file",
            "description": "Get detailed information about a specific STF file",
            "parameters": ["file_id", "stf_filename"],
        },
        {
            "name": "swf_list_tf_slices",
            "description": "List TF slices for fast processing workflow",
            "parameters": ["run_number", "stf_filename", "tf_filename", "status", "assigned_worker", "start_time", "end_time"],
        },
        {
            "name": "swf_get_tf_slice",
            "description": "Get detailed information about a specific TF slice",
            "parameters": ["tf_filename", "slice_id"],
        },
        {
            "name": "swf_list_logs",
            "description": "List application log entries. Use level='ERROR' to find errors",
            "parameters": ["app_name", "instance_name", "execution_id", "level", "search", "start_time", "end_time"],
        },
        {
            "name": "swf_get_log_entry",
            "description": "Get full details of a specific log entry",
            "parameters": ["log_id"],
        },
        {
            "name": "swf_start_workflow",
            "description": "Start a workflow by sending command to DAQ Simulator agent",
            "parameters": ["workflow_name", "namespace", "config", "realtime", "duration",
                          "stf_count", "physics_period_count", "physics_period_duration", "stf_interval"],
        },
        {
            "name": "swf_stop_workflow",
            "description": "Stop a running workflow by sending stop command to agent",
            "parameters": ["execution_id"],
        },
        {
            "name": "swf_end_execution",
            "description": "Mark a workflow execution as terminated in database (no agent message)",
            "parameters": ["execution_id"],
        },
        {
            "name": "swf_kill_agent",
            "description": "Kill an agent process by sending SIGKILL to its PID. Sets status to EXITED.",
            "parameters": ["name"],
        },
        {
            "name": "swf_check_agent_manager",
            "description": "Check if user's agent manager daemon is alive (has recent heartbeat)",
            "parameters": ["username"],
        },
        {
            "name": "swf_start_user_testbed",
            "description": "Start user's testbed via their agent manager daemon",
            "parameters": ["username", "config_name"],
        },
        {
            "name": "swf_stop_user_testbed",
            "description": "Stop user's testbed via their agent manager daemon",
            "parameters": ["username"],
        },
        {
            "name": "swf_get_testbed_status",
            "description": "Get comprehensive testbed status: agent manager, namespace, workflow agents",
            "parameters": ["username"],
        },
        {
            "name": "swf_get_workflow_monitor",
            "description": "Get status and events for a workflow execution (aggregates messages/logs)",
            "parameters": ["execution_id"],
        },
        {
            "name": "swf_list_workflow_monitors",
            "description": "List recent workflow executions that can be monitored",
            "parameters": [],
        },
        {
            "name": "swf_send_message",
            "description": "Send a message to the monitoring stream (for testing, announcements, etc.)",
            "parameters": ["message", "message_type", "metadata"],
        },
        {
            "name": "swf_record_ai_memory",
            "description": "Record a dialogue exchange for AI memory persistence",
            "parameters": ["username", "session_id", "role", "content", "namespace", "project_path"],
        },
        {
            "name": "swf_get_ai_memory",
            "description": "Get recent dialogue history for session context",
            "parameters": ["username", "turns", "namespace"],
        },
        # PanDA Monitor tools
        {
            "name": "panda_list_jobs",
            "description": "List PanDA jobs from ePIC production DB with summary stats. Cursor-based pagination via before_id.",
            "parameters": ["days", "status", "username", "site", "taskid", "reqid", "limit", "before_id"],
        },
        {
            "name": "panda_diagnose_jobs",
            "description": "Diagnose failed/faulty PanDA jobs with full error details (7 error components). Cursor-based pagination via before_id.",
            "parameters": ["days", "username", "site", "taskid", "reqid", "error_component", "limit", "before_id"],
        },
        {
            "name": "panda_list_tasks",
            "description": "List JEDI tasks from ePIC production DB with summary stats. Tasks are higher-level than jobs. Cursor-based pagination via before_id.",
            "parameters": ["days", "status", "username", "taskname", "reqid", "workinggroup", "taskid", "limit", "before_id"],
        },
        {
            "name": "panda_error_summary",
            "description": "Aggregate error summary across failed PanDA jobs, ranked by frequency. Shows most common errors with affected tasks, users, sites.",
            "parameters": ["days", "username", "site", "taskid", "error_source", "limit"],
        },
    ]
