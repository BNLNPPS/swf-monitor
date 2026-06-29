"""
MCP Tools for ePIC Streaming Workflow Testbed Monitor and PanDA Monitor.

This package provides LLM-based natural language interaction with the testbed
and the PanDA production system, allowing users to query system state, agents,
workflows, runs, STF files, TF slices, messages, PanDA jobs, error diagnostics,
and manage AI dialogue memory.

ARCHITECTURE PRINCIPLE:
- Monitor consumes ALL workflow messages from ActiveMQ
- MCP provides access to everything monitor captures
- Use MCP tools for diagnostics, NOT log files
- PanDA MCP tools query the doma_panda schema directly for ePIC production monitoring

Module structure:
- common.py: Shared utilities and tool discovery list
- system.py: System state, agents, namespaces, logs, testbed management
- workflows.py: Workflow definitions, executions, messages, runs, files, slices
- ai_memory.py: AI dialogue recording and retrieval for session context
- ai_content.py: AI assessment registration and retrieval for production objects
- pandamon.py: PanDA job monitoring and error diagnostics for ePIC production
- pcs.py: PCS (Physics Configuration System) tag browsing and lookup
"""

from django.conf import settings
from mcp.server.fastmcp import FastMCP

# Single FastMCP instance shared by every @mcp.tool() in this package and
# by the standalone ASGI entrypoint in swf_monitor_project/mcp_asgi.py.
# Tool modules in this package import this same `mcp` symbol via
# `from monitor_app.mcp import mcp`. See docs/MCP_FASTMCP_MIGRATION_PLAN.md.
mcp = FastMCP(
    settings.MCP_SERVER_NAME,
    instructions=settings.MCP_SERVER_INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
)


@mcp.tool()
async def get_server_instructions() -> str:
    """Get the swf-monitor MCP server instructions.

    Compatibility tool for clients and permissions lists that previously
    used django-mcp-server's server-instruction helper.
    """
    return settings.MCP_SERVER_INSTRUCTIONS


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

# AI Content tools
from .ai_content import (
    epic_register_ai_assessment,
    epic_get_ai_content,
)

# PanDA Monitor tools
from .pandamon import (
    panda_list_jobs,
    panda_diagnose_jobs,
    panda_list_tasks,
    panda_error_summary,
    panda_get_activity,
    panda_study_job,
)

# PCS tools
from .pcs import (
    pcs_list_tags,
    pcs_get_tag,
    pcs_search_tags,
    pcs_dataset_list,
    pcs_dataset_get,
    pcs_dataset_intake,
    pcs_prodtask_list,
    pcs_prodtask_get,
    pcs_prodtask_artifact,
    pcs_prodtask_intake,
    pcs_prodtask_link_input,
    pcs_prodtask_set_status,
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
    'get_server_instructions',
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
    # AI Content
    'epic_register_ai_assessment',
    'epic_get_ai_content',
    # PanDA Monitor
    'panda_list_jobs',
    'panda_diagnose_jobs',
    'panda_list_tasks',
    'panda_error_summary',
    'panda_get_activity',
    'panda_study_job',
    # PCS — tag browsing
    'pcs_list_tags',
    'pcs_get_tag',
    'pcs_search_tags',
    # PCS — datasets and tasks
    'pcs_dataset_list',
    'pcs_dataset_get',
    'pcs_dataset_intake',
    'pcs_prodtask_list',
    'pcs_prodtask_get',
    'pcs_prodtask_artifact',
    'pcs_prodtask_intake',
    'pcs_prodtask_link_input',
    'pcs_prodtask_set_status',
]
