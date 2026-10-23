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
- ai_proposals.py: AI proposal listing and human-decision relay (bot review flow)
- epicprod_actions.py: epicprod action-stream retrieval (structured action log)
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


EXTERNAL_INSTRUCTIONS = """swf-monitor MCP, external face (epic-devcloud.org).

You are connected through swf-remote as a named collaborator; the tools
run as that person. This face serves the read-only tools of the ePIC
production monitor: PanDA tasks, jobs, queues, errors, and resource usage
(panda_*), the production catalog and physics configurations (pcs_*
reads), the production action stream (epicprod_*), Snapper state history
(snapper_*), both Rucio catalogs (bnl_rucio_*, jlab_rucio_*), AI
assessments and proposals (epic_get_ai_content, ai_list_proposals), and
one write: ai_propose_ping, which proposes a dated obligation in natural
language; it takes effect only when a person accepts it on the alarm
dashboard.

The tool catalog is tools/list; call it rather than looking for a listing
tool. Decisions on proposals, task operations, and every other mutation
happen on the web pages by signed-in people, not through this face.
The human-facing outline is
https://epic-wfms-docs.readthedocs.io/en/latest/apis/#mcp
"""


@mcp.tool()
async def get_server_instructions() -> str:
    """Get the swf-monitor MCP server instructions: what this toolset
    is and how to use it. Through the external face (epic-devcloud.org)
    the instructions describe the tools served there and name tools/list
    as the catalog.
    """
    from .common import CALLER
    if CALLER.get():
        return EXTERNAL_INSTRUCTIONS
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

# AI proposal tools

# Domain tools and assessment subject types, hosted in swf-epicprod.
# The import is the registration shim: loading the package registers its
# tools on this package's `mcp` instance and its subject resolvers on
# ai_content's registry — one MCP service downstream either way. A plain
# import (not from-import) keeps both package-entry orders safe.
import swf_epicprod.mcp_tools  # noqa: F401  (registration side effect)

# JLab science data and BNL PanDA output/log catalog tools. Both catalog
# credentials remain local to swf-testbed; this import registers prefixed,
# read-only wrappers on the authenticated SWF MCP service.
from .rucio import RUCIO_TOOL_NAMES  # noqa: E402

# PanDA Monitor tools
from .pandamon import (
    panda_list_jobs,
    panda_diagnose_jobs,
    panda_list_tasks,
    panda_error_summary,
    panda_get_activity,
    panda_study_job,
)

# Snapper temporal-query tools (snapper-ai PLAN.md Phase 5)
from .snapper import (
    snapper_latest,
    snapper_state_at,
    snapper_component_history,
    snapper_changes_between,
    snapper_context_around,
    snapper_series,
    snapper_cut_summary,
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
    # AI Proposals
    'ai_list_proposals',
    'ai_decide_proposal',
    # PanDA Monitor
    'panda_list_jobs',
    'panda_diagnose_jobs',
    'panda_list_tasks',
    'panda_error_summary',
    'panda_get_activity',
    'panda_study_job',
    # Snapper state history
    'snapper_latest',
    'snapper_state_at',
    'snapper_component_history',
    'snapper_changes_between',
    'snapper_context_around',
    'snapper_series',
    'snapper_cut_summary',
    # Rucio catalogs
    *RUCIO_TOOL_NAMES,
    # PCS — tag browsing
    'pcs_list_tags',
    'pcs_get_tag',
    'pcs_search_tags',
    # PCS — datasets and tasks
    'pcs_dataset_list',
    'pcs_dataset_get',
    'pcs_data_provenance',
    'pcs_dataset_intake',
    'pcs_prodtask_list',
    'pcs_prodtask_get',
    'pcs_prodtask_artifact',
    'pcs_prodtask_intake',
    'pcs_prodtask_link_input',
    'pcs_prodtask_set_status',
]
