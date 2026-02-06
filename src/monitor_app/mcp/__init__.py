"""
MCP Tools for ePIC Streaming Workflow Testbed Monitor.

This package provides LLM-based natural language interaction with the testbed,
allowing users to query system state, agents, workflows, runs, STF files,
TF slices, messages, and manage AI dialogue memory.

ARCHITECTURE PRINCIPLE:
- Monitor consumes ALL workflow messages from ActiveMQ
- MCP provides access to everything monitor captures
- Use MCP tools for diagnostics, NOT log files

Module structure:
- common.py: Shared utilities and tool discovery list
- system.py: System state, agents, namespaces, logs, testbed management
- workflows.py: Workflow definitions, executions, messages, runs, files, slices
- ai_memory.py: AI dialogue recording and retrieval for session context
"""

from mcp_server import mcp_server as mcp

# Import common utilities
from .common import (
    _parse_time,
    _default_start_time,
    _monitor_url,
    _get_testbed_config_path,
    get_available_tools_list,
)

# Import all tools to register them with the MCP server
# System tools
from .system import (
    swf_get_system_state,
    swf_list_agents,
    swf_get_agent,
    swf_list_namespaces,
    swf_get_namespace,
    swf_list_logs,
    swf_get_log_entry,
    swf_kill_agent,
    swf_check_agent_manager,
    swf_start_user_testbed,
    swf_stop_user_testbed,
    swf_get_testbed_status,
)

# Workflow tools
from .workflows import (
    swf_list_workflow_definitions,
    swf_list_workflow_executions,
    swf_get_workflow_execution,
    swf_list_messages,
    swf_list_runs,
    swf_get_run,
    swf_list_stf_files,
    swf_get_stf_file,
    swf_list_tf_slices,
    swf_get_tf_slice,
    swf_start_workflow,
    swf_stop_workflow,
    swf_end_execution,
    swf_get_workflow_monitor,
    swf_list_workflow_monitors,
    swf_send_message,
)

# AI Memory tools
from .ai_memory import (
    swf_record_ai_memory,
    swf_get_ai_memory,
)


# Tool discovery - registered as MCP tool
@mcp.tool()
async def swf_list_available_tools() -> list:
    """
    List all available MCP tools with descriptions.

    Use this tool to discover what tools are available and what they do.
    Returns a summary of each tool to help you choose the right one.

    Returns list of tools with: name, description, parameters
    """
    return get_available_tools_list()


# Export all tools for backward compatibility
__all__ = [
    # Discovery
    'swf_list_available_tools',
    # System
    'swf_get_system_state',
    'swf_list_agents',
    'swf_get_agent',
    'swf_list_namespaces',
    'swf_get_namespace',
    'swf_list_logs',
    'swf_get_log_entry',
    'swf_kill_agent',
    'swf_check_agent_manager',
    'swf_start_user_testbed',
    'swf_stop_user_testbed',
    'swf_get_testbed_status',
    # Workflows
    'swf_list_workflow_definitions',
    'swf_list_workflow_executions',
    'swf_get_workflow_execution',
    'swf_list_messages',
    'swf_list_runs',
    'swf_get_run',
    'swf_list_stf_files',
    'swf_get_stf_file',
    'swf_list_tf_slices',
    'swf_get_tf_slice',
    'swf_start_workflow',
    'swf_stop_workflow',
    'swf_end_execution',
    'swf_get_workflow_monitor',
    'swf_list_workflow_monitors',
    'swf_send_message',
    # AI Memory
    'swf_record_ai_memory',
    'swf_get_ai_memory',
]
