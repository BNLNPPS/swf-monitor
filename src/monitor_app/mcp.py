"""
MCP Tools for ePIC Streaming Workflow Testbed Monitor.

These tools enable LLM-based natural language interaction with the testbed,
allowing users to query system state, agents, workflows, runs, STF files,
TF slices, and messages.

ARCHITECTURE PRINCIPLE:
- Monitor consumes ALL workflow messages from ActiveMQ
- MCP provides access to everything monitor captures
- Use MCP tools for diagnostics, NOT log files

TESTBED MANAGEMENT:
- get_testbed_status(username) - Comprehensive status: agent manager, namespace, agents
- start_user_testbed(username, config_name) - Start testbed with config (default: testbed.toml)
- stop_user_testbed(username) - Stop all workflow agents
- check_agent_manager(username) - Check if agent manager daemon is alive

DIAGNOSTIC TOOLS:
- list_logs(instance_name='...') - Get logs for specific agent
- list_logs(level='ERROR') - Find workflow failures and errors
- list_messages(execution_id='...') - Track workflow progress

WORKFLOW OPERATIONS:
- start_workflow() - Start workflow using defaults from testbed.toml
- stop_workflow(execution_id) - Stop a running workflow
- get_workflow_monitor(execution_id) - Get workflow status and events

Design Philosophy:
- Tools are data access primitives with filtering capabilities
- The LLM synthesizes, summarizes, and aggregates information
- All list tools support start_time/end_time for date range filtering
- Context filters cascade: run_number -> stf_filename -> tf_filename
"""

import logging

from datetime import timedelta
from django.utils import timezone

logger = logging.getLogger(__name__)
from django.utils.dateparse import parse_datetime
from django.db.models import Count
from mcp_server import mcp_server as mcp

from .models import (
    SystemAgent,
    Run,
    StfFile,
    TFSlice,
    RunState,
    PersistentState,
    SystemStateEvent,
    AppLog,
)
from .workflow_models import (
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowMessage,
    Namespace,
)


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

@mcp.tool()
async def list_available_tools() -> list:
    """
    List all available MCP tools with descriptions.

    Use this tool to discover what tools are available and what they do.
    Returns a summary of each tool to help you choose the right one.

    Returns list of tools with: name, description, parameters
    """
    tools = [
        {
            "name": "list_available_tools",
            "description": "List all available MCP tools with descriptions",
            "parameters": [],
        },
        {
            "name": "get_system_state",
            "description": "Get comprehensive system state: user context, agent manager, workflow runner, readiness, agents, executions",
            "parameters": ["username"],
        },
        {
            "name": "list_agents",
            "description": "List registered agents. Excludes EXITED agents by default. Use status='EXITED' to see exited, status='all' to see all.",
            "parameters": ["namespace", "agent_type", "status", "execution_id", "start_time", "end_time"],
        },
        {
            "name": "get_agent",
            "description": "Get detailed information about a specific agent",
            "parameters": ["name"],
        },
        {
            "name": "list_namespaces",
            "description": "List all testbed namespaces (isolation boundaries for users)",
            "parameters": [],
        },
        {
            "name": "get_namespace",
            "description": "Get namespace details including activity counts",
            "parameters": ["namespace", "start_time", "end_time"],
        },
        {
            "name": "list_workflow_definitions",
            "description": "List available workflow definitions that can be executed",
            "parameters": ["workflow_type", "created_by"],
        },
        {
            "name": "list_workflow_executions",
            "description": "List workflow executions. Use currently_running=True to see what's running now",
            "parameters": ["namespace", "status", "executed_by", "workflow_name", "currently_running", "start_time", "end_time"],
        },
        {
            "name": "get_workflow_execution",
            "description": "Get detailed information about a specific workflow execution",
            "parameters": ["execution_id"],
        },
        {
            "name": "list_messages",
            "description": "List workflow messages between agents for debugging",
            "parameters": ["namespace", "execution_id", "agent", "message_type", "start_time", "end_time"],
        },
        {
            "name": "list_runs",
            "description": "List simulation runs with timing and STF file counts",
            "parameters": ["start_time", "end_time"],
        },
        {
            "name": "get_run",
            "description": "Get detailed information about a specific run",
            "parameters": ["run_number"],
        },
        {
            "name": "list_stf_files",
            "description": "List STF (Super Time Frame) files with filtering",
            "parameters": ["run_number", "status", "machine_state", "start_time", "end_time"],
        },
        {
            "name": "get_stf_file",
            "description": "Get detailed information about a specific STF file",
            "parameters": ["file_id", "stf_filename"],
        },
        {
            "name": "list_tf_slices",
            "description": "List TF slices for fast processing workflow",
            "parameters": ["run_number", "stf_filename", "tf_filename", "status", "assigned_worker", "start_time", "end_time"],
        },
        {
            "name": "get_tf_slice",
            "description": "Get detailed information about a specific TF slice",
            "parameters": ["tf_filename", "slice_id"],
        },
        {
            "name": "list_logs",
            "description": "List application log entries. Use level='ERROR' to find errors",
            "parameters": ["app_name", "instance_name", "level", "search", "start_time", "end_time"],
        },
        {
            "name": "get_log_entry",
            "description": "Get full details of a specific log entry",
            "parameters": ["log_id"],
        },
        {
            "name": "start_workflow",
            "description": "Start a workflow by sending command to DAQ Simulator agent",
            "parameters": ["workflow_name", "namespace", "config", "realtime", "duration",
                          "stf_count", "physics_period_count", "physics_period_duration", "stf_interval"],
        },
        {
            "name": "stop_workflow",
            "description": "Stop a running workflow by sending stop command to agent",
            "parameters": ["execution_id"],
        },
        {
            "name": "end_execution",
            "description": "Mark a workflow execution as terminated in database (no agent message)",
            "parameters": ["execution_id"],
        },
        {
            "name": "kill_agent",
            "description": "Kill an agent process by sending SIGKILL to its PID. Sets status to EXITED.",
            "parameters": ["name"],
        },
        {
            "name": "check_agent_manager",
            "description": "Check if user's agent manager daemon is alive (has recent heartbeat)",
            "parameters": ["username"],
        },
        {
            "name": "start_user_testbed",
            "description": "Start user's testbed via their agent manager daemon",
            "parameters": ["username", "config_name"],
        },
        {
            "name": "stop_user_testbed",
            "description": "Stop user's testbed via their agent manager daemon",
            "parameters": ["username"],
        },
        {
            "name": "get_testbed_status",
            "description": "Get comprehensive testbed status: agent manager, namespace, workflow agents",
            "parameters": ["username"],
        },
        {
            "name": "get_workflow_monitor",
            "description": "Get status and events for a workflow execution (aggregates messages/logs)",
            "parameters": ["execution_id"],
        },
        {
            "name": "list_workflow_monitors",
            "description": "List recent workflow executions that can be monitored",
            "parameters": [],
        },
    ]
    return tools


# -----------------------------------------------------------------------------
# System State
# -----------------------------------------------------------------------------

@mcp.tool()
async def get_system_state(username: str = None) -> dict:
    """
    Get comprehensive system state including agents, executions, run states, and persistent state.

    Use this tool first to get a high-level view of the entire system before drilling
    into specific details. This is the starting point for understanding testbed health.

    Args:
        username: Username to get context for (reads their testbed.toml).
                  If not provided, uses SWF_HOME environment variable.

    Returns:
    - timestamp: current server time
    - user_context: namespace, workflow defaults from testbed.toml
    - agent_manager: status of user's agent manager daemon
    - workflow_runner: status of healthy DAQ_Simulator that can accept start_workflow
    - ready_to_run: boolean - True if workflow_runner is healthy
    - last_execution: most recent workflow execution for user's namespace
    - errors_last_hour: count of ERROR logs in user's namespace
    - agents: total, healthy (heartbeat <5min), unhealthy counts
    - executions: running count, completed in last hour
    - messages: count in last 10 minutes
    - run_states: current fast processing run states
    - persistent_state: system-wide persistent state (next IDs, etc.)
    - recent_events: last 10 system state events
    """
    import os
    from pathlib import Path
    from asgiref.sync import sync_to_async

    # Determine username and SWF_HOME path
    if username:
        # Compute path from username pattern: /data/{username}/github
        swf_home = f'/data/{username}/github'
    else:
        swf_home = os.getenv('SWF_HOME', '')
        # Try to extract username from SWF_HOME path
        if swf_home and '/data/' in swf_home:
            parts = swf_home.split('/')
            try:
                idx = parts.index('data')
                if idx + 1 < len(parts):
                    username = parts[idx + 1]
            except (ValueError, IndexError):
                pass
        if not username:
            import getpass
            username = getpass.getuser()

    @sync_to_async
    def fetch():
        now = timezone.now()
        recent_threshold = now - timedelta(minutes=5)

        # --- User context from testbed config ---
        testbed_toml, config_source = _get_testbed_config_path()
        user_context = {
            "username": username,
            "namespace": None,
            "workflow_name": None,
            "config": None,
            "config_file": str(testbed_toml.name) if testbed_toml else None,
            "config_source": config_source,
        }
        if testbed_toml and testbed_toml.exists():
            try:
                import tomllib
                with open(testbed_toml, 'rb') as f:
                    toml_data = tomllib.load(f)
                user_context["namespace"] = toml_data.get('testbed', {}).get('namespace')
                workflow_section = toml_data.get('workflow', {})
                user_context["workflow_name"] = workflow_section.get('name')
                user_context["config"] = workflow_section.get('config')
            except Exception:
                pass

        user_namespace = user_context.get("namespace")

        # --- Agent manager status ---
        agent_manager_name = f'agent-manager-{username}'
        agent_manager = {"status": "missing", "last_heartbeat": None}
        try:
            am = SystemAgent.objects.get(instance_name=agent_manager_name)
            if am.operational_state == 'EXITED':
                agent_manager["status"] = "exited"
            elif am.last_heartbeat and am.last_heartbeat >= recent_threshold:
                agent_manager["status"] = "healthy"
            else:
                agent_manager["status"] = "unhealthy"
            agent_manager["last_heartbeat"] = am.last_heartbeat.isoformat() if am.last_heartbeat else None
        except SystemAgent.DoesNotExist:
            pass

        # --- Workflow runner status (DAQ_Simulator in user's namespace) ---
        workflow_runner = {"status": "missing", "name": None, "last_heartbeat": None}
        runner_qs = SystemAgent.objects.filter(
            agent_type__in=['DAQ_Simulator', 'workflow_runner'],
            namespace=user_namespace,
            last_heartbeat__gte=recent_threshold,
        ).exclude(operational_state='EXITED').order_by('-last_heartbeat')

        if runner_qs.exists():
            runner = runner_qs.first()
            workflow_runner["status"] = "healthy"
            workflow_runner["name"] = runner.instance_name
            workflow_runner["last_heartbeat"] = runner.last_heartbeat.isoformat() if runner.last_heartbeat else None
        else:
            # Check if there's one but unhealthy
            any_runner = SystemAgent.objects.filter(
                agent_type__in=['DAQ_Simulator', 'workflow_runner'],
                namespace=user_namespace,
            ).exclude(operational_state='EXITED').first()
            if any_runner:
                workflow_runner["status"] = "unhealthy"
                workflow_runner["name"] = any_runner.instance_name
                workflow_runner["last_heartbeat"] = any_runner.last_heartbeat.isoformat() if any_runner.last_heartbeat else None

        # --- Ready to run ---
        ready_to_run = workflow_runner["status"] == "healthy"

        # --- Last execution for user's namespace ---
        last_execution = None
        if user_namespace:
            last_exec = WorkflowExecution.objects.filter(
                namespace=user_namespace
            ).order_by('-start_time').first()
            if last_exec:
                last_execution = {
                    "execution_id": last_exec.execution_id,
                    "status": last_exec.status,
                    "start_time": last_exec.start_time.isoformat() if last_exec.start_time else None,
                    "end_time": last_exec.end_time.isoformat() if last_exec.end_time else None,
                }

        # --- Errors in last hour for user's namespace ---
        errors_last_hour = 0
        if user_namespace:
            import logging as py_logging
            errors_last_hour = AppLog.objects.filter(
                level__gte=py_logging.ERROR,
                timestamp__gte=now - timedelta(hours=1),
                extra_data__namespace=user_namespace,
            ).count()

        # --- Global agent stats ---
        total_agents = SystemAgent.objects.count()
        exited_agents = SystemAgent.objects.filter(operational_state='EXITED').count()
        active_agents = total_agents - exited_agents

        healthy_agents = SystemAgent.objects.filter(
            last_heartbeat__gte=recent_threshold,
            status='OK'
        ).exclude(operational_state='EXITED').count()

        # Execution stats
        running_executions = WorkflowExecution.objects.filter(status='running').count()
        recent_completed = WorkflowExecution.objects.filter(
            status='completed',
            end_time__gte=now - timedelta(hours=1)
        ).count()

        # Message stats
        recent_messages = WorkflowMessage.objects.filter(
            sent_at__gte=now - timedelta(minutes=10)
        ).count()

        # Run states (fast processing)
        run_states = [
            {
                "run_number": rs.run_number,
                "phase": rs.phase,
                "state": rs.state,
                "substate": rs.substate,
                "active_workers": rs.active_worker_count,
                "target_workers": rs.target_worker_count,
                "slices_queued": rs.slices_queued,
                "slices_processing": rs.slices_processing,
                "slices_completed": rs.slices_completed,
                "slices_failed": rs.slices_failed,
            }
            for rs in RunState.objects.all().order_by('-run_number')[:5]
        ]

        # Persistent state
        persistent_state = PersistentState.get_state()

        # Recent system events
        recent_events = [
            {
                "timestamp": e.timestamp.isoformat(),
                "run_number": e.run_number,
                "event_type": e.event_type,
                "state": e.state,
            }
            for e in SystemStateEvent.objects.order_by('-timestamp')[:10]
        ]

        return {
            "timestamp": now.isoformat(),
            "user_context": user_context,
            "agent_manager": agent_manager,
            "workflow_runner": workflow_runner,
            "ready_to_run": ready_to_run,
            "last_execution": last_execution,
            "errors_last_hour": errors_last_hour,
            "agents": {
                "total": total_agents,
                "active": active_agents,
                "exited": exited_agents,
                "healthy": healthy_agents,
                "unhealthy": active_agents - healthy_agents,
            },
            "executions": {
                "running": running_executions,
                "completed_last_hour": recent_completed,
            },
            "messages_last_10min": recent_messages,
            "run_states": run_states,
            "persistent_state": persistent_state,
            "recent_events": recent_events,
        }

    return await fetch()


# -----------------------------------------------------------------------------
# Agents
# -----------------------------------------------------------------------------

@mcp.tool()
async def list_agents(
    namespace: str = None,
    agent_type: str = None,
    status: str = None,
    execution_id: str = None,
    start_time: str = None,
    end_time: str = None,
) -> list:
    """
    List registered agents with filtering options.

    Agents are processes that participate in workflows (DAQ simulator, data agent,
    processing agent, fast monitoring agent). Each sends periodic heartbeats.

    By default, excludes EXITED agents. Use status='EXITED' to see only exited,
    or status='all' to see all agents regardless of status.

    Args:
        namespace: Filter to agents in this namespace (e.g., 'torre1', 'wenauseic')
        agent_type: Filter by type: 'daqsim', 'data', 'processing', 'fastmon', 'workflow_runner'
        status: Filter by status: 'OK', 'WARNING', 'ERROR', 'UNKNOWN', 'EXITED', or 'all'.
                Default (None) excludes EXITED agents.
        execution_id: Filter to agents that participated in this workflow execution
        start_time: Filter to agents with heartbeat >= this ISO datetime
        end_time: Filter to agents with heartbeat <= this ISO datetime

    Returns list of agents with: name, agent_type, status, operational_state, namespace,
    last_heartbeat, workflow_enabled, total_stf_processed
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        qs = SystemAgent.objects.all().order_by('-last_heartbeat')

        if namespace:
            qs = qs.filter(namespace=namespace)
        if agent_type:
            qs = qs.filter(agent_type=agent_type)

        # Status filtering: default excludes EXITED
        if status is None:
            qs = qs.exclude(status='EXITED')
        elif status.lower() != 'all':
            qs = qs.filter(status__iexact=status)

        # Date range filter on heartbeat
        start = _parse_time(start_time)
        end = _parse_time(end_time)
        if start:
            qs = qs.filter(last_heartbeat__gte=start)
        if end:
            qs = qs.filter(last_heartbeat__lte=end)

        # Filter by execution participation (via messages)
        if execution_id:
            agent_names = WorkflowMessage.objects.filter(
                execution_id=execution_id
            ).values_list('sender_agent', flat=True).distinct()
            qs = qs.filter(instance_name__in=agent_names)

        # Build URL with filters
        params = []
        if namespace:
            params.append(f"namespace={namespace}")
        if agent_type:
            params.append(f"agent_type={agent_type}")
        if status and status.lower() != 'all':
            params.append(f"status={status}")
        query_string = "&".join(params)
        url = _monitor_url(f"/agents/?{query_string}" if query_string else "/agents/")

        return {
            "items": [
                {
                    "name": a.instance_name,
                    "agent_type": a.agent_type,
                    "status": a.status,
                    "operational_state": a.operational_state,
                    "namespace": a.namespace,
                    "last_heartbeat": a.last_heartbeat.isoformat() if a.last_heartbeat else None,
                    "workflow_enabled": a.workflow_enabled,
                    "total_stf_processed": a.total_stf_processed,
                }
                for a in qs
            ],
            "monitor_urls": [
                {"title": "Agents List", "url": url},
            ],
        }

    return await fetch()


@mcp.tool()
async def get_agent(name: str) -> dict:
    """
    Get detailed information about a specific agent.

    Use list_agents first to see available agent names if you don't know them.

    Args:
        name: The exact agent instance name (e.g., 'daq_simulator_torre1')

    Returns: name, agent_type, status, namespace, last_heartbeat, description,
    workflow_enabled, current_stf_count, total_stf_processed, metadata
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        try:
            a = SystemAgent.objects.get(instance_name=name)
            return {
                "name": a.instance_name,
                "agent_type": a.agent_type,
                "status": a.status,
                "namespace": a.namespace,
                "last_heartbeat": a.last_heartbeat.isoformat() if a.last_heartbeat else None,
                "description": a.description,
                "workflow_enabled": a.workflow_enabled,
                "current_stf_count": a.current_stf_count,
                "total_stf_processed": a.total_stf_processed,
                "last_stf_processed": a.last_stf_processed.isoformat() if a.last_stf_processed else None,
                "metadata": a.metadata,
                "monitor_urls": [
                    {"title": "Agent Detail", "url": _monitor_url(f"/agents/{a.instance_name}/")},
                ],
            }
        except SystemAgent.DoesNotExist:
            return {"error": f"Agent '{name}' not found. Use list_agents to see available agents."}

    return await fetch()


# -----------------------------------------------------------------------------
# Namespaces
# -----------------------------------------------------------------------------

@mcp.tool()
async def list_namespaces() -> list:
    """
    List all testbed namespaces.

    Namespaces provide isolation between different users' workflow runs.
    Each namespace has its own set of agents and workflow executions.

    Returns list of namespaces with: name, owner, description
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        return {
            "items": [
                {
                    "name": n.name,
                    "owner": n.owner,
                    "description": n.description,
                }
                for n in Namespace.objects.all().order_by('name')
            ],
            "monitor_urls": [
                {"title": "Namespaces List", "url": _monitor_url("/namespaces/")},
            ],
        }

    return await fetch()


@mcp.tool()
async def get_namespace(
    namespace: str,
    start_time: str = None,
    end_time: str = None,
) -> dict:
    """
    Get detailed information about a namespace including activity counts.

    Args:
        namespace: The namespace name (required)
        start_time: Count activity from this ISO datetime (default: last 24 hours)
        end_time: Count activity until this ISO datetime (default: now)

    Returns: name, owner, description, agent_count, execution_count, message_count,
    active_users (users who ran executions in the time range)
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        # Get namespace record
        try:
            ns = Namespace.objects.get(name=namespace)
            ns_info = {
                "name": ns.name,
                "owner": ns.owner,
                "description": ns.description,
            }
        except Namespace.DoesNotExist:
            ns_info = {
                "name": namespace,
                "owner": None,
                "description": None,
            }

        # Date range defaults
        start = _parse_time(start_time) or _default_start_time(24)
        end = _parse_time(end_time) or timezone.now()

        # Count agents
        agent_count = SystemAgent.objects.filter(namespace=namespace).count()

        # Count executions in time range
        execution_qs = WorkflowExecution.objects.filter(
            namespace=namespace,
            start_time__gte=start,
            start_time__lte=end,
        )
        execution_count = execution_qs.count()

        # Get active users
        active_users = list(execution_qs.values_list('executed_by', flat=True).distinct())

        # Count messages in time range
        message_count = WorkflowMessage.objects.filter(
            namespace=namespace,
            sent_at__gte=start,
            sent_at__lte=end,
        ).count()

        return {
            **ns_info,
            "agent_count": agent_count,
            "execution_count": execution_count,
            "message_count": message_count,
            "active_users": active_users,
            "time_range": {
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            "monitor_urls": [
                {"title": "Namespace Detail", "url": _monitor_url(f"/namespaces/{namespace}/")},
            ],
        }

    return await fetch()


# -----------------------------------------------------------------------------
# Workflow Definitions
# -----------------------------------------------------------------------------

@mcp.tool()
async def list_workflow_definitions(
    workflow_type: str = None,
    created_by: str = None,
) -> list:
    """
    List available workflow definitions that can be executed.

    Workflow definitions describe the structure of a workflow (stages, agents needed).
    Common workflows include 'stf_datataking' for streaming data acquisition simulation.

    Args:
        workflow_type: Filter by type (e.g., 'simulation', 'production')
        created_by: Filter by creator username

    Returns list of definitions with: workflow_name, version, workflow_type,
    created_by, created_at, execution_count
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        qs = WorkflowDefinition.objects.annotate(
            execution_count=Count('executions')
        ).order_by('workflow_name', '-version')

        if workflow_type:
            qs = qs.filter(workflow_type=workflow_type)
        if created_by:
            qs = qs.filter(created_by=created_by)

        return {
            "items": [
                {
                    "workflow_name": w.workflow_name,
                    "version": w.version,
                    "workflow_type": w.workflow_type,
                    "created_by": w.created_by,
                    "created_at": w.created_at.isoformat() if w.created_at else None,
                    "execution_count": w.execution_count,
                }
                for w in qs
            ],
            "monitor_urls": [
                {"title": "Workflow Definitions", "url": _monitor_url("/definitions/")},
            ],
        }

    return await fetch()


# -----------------------------------------------------------------------------
# Workflow Executions
# -----------------------------------------------------------------------------

@mcp.tool()
async def list_workflow_executions(
    namespace: str = None,
    status: str = None,
    executed_by: str = None,
    workflow_name: str = None,
    currently_running: bool = False,
    start_time: str = None,
    end_time: str = None,
) -> list:
    """
    List workflow executions with filtering.

    Args:
        namespace: Filter to executions in this namespace
        status: Filter by status: 'pending', 'running', 'completed', 'failed', 'terminated'
        executed_by: Filter by user who started the execution
        workflow_name: Filter by workflow definition name
        currently_running: If True, return all running executions (ignores date range)
        start_time: Filter executions started >= this ISO datetime (default: last 24 hours)
        end_time: Filter executions started <= this ISO datetime

    Returns list of executions with: execution_id, workflow_name, namespace,
    status, executed_by, start_time, end_time, parameter_values
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        qs = WorkflowExecution.objects.select_related('workflow_definition').order_by('-start_time')

        if namespace:
            qs = qs.filter(namespace=namespace)
        if currently_running:
            qs = qs.filter(status__iexact='running')
        elif status:
            qs = qs.filter(status__iexact=status)
        if executed_by:
            qs = qs.filter(executed_by=executed_by)
        if workflow_name:
            qs = qs.filter(workflow_definition__workflow_name=workflow_name)

        # Date range filter (skip if currently_running)
        if not currently_running:
            start = _parse_time(start_time) or _default_start_time(24)
            end = _parse_time(end_time)
            qs = qs.filter(start_time__gte=start)
            if end:
                qs = qs.filter(start_time__lte=end)

        # Build URL with filters
        params = []
        if namespace:
            params.append(f"namespace={namespace}")
        if status:
            params.append(f"status={status}")
        if executed_by:
            params.append(f"executed_by={executed_by}")
        query_string = "&".join(params)
        url = _monitor_url(f"/executions/?{query_string}" if query_string else "/executions/")

        return {
            "items": [
                {
                    "execution_id": e.execution_id,
                    "workflow_name": e.workflow_definition.workflow_name if e.workflow_definition else None,
                    "namespace": e.namespace,
                    "status": e.status,
                    "executed_by": e.executed_by,
                    "start_time": e.start_time.isoformat() if e.start_time else None,
                    "end_time": e.end_time.isoformat() if e.end_time else None,
                    "parameter_values": e.parameter_values,
                }
                for e in qs[:100]
            ],
            "monitor_urls": [
                {"title": "Executions List", "url": url},
            ],
        }

    return await fetch()


@mcp.tool()
async def get_workflow_execution(execution_id: str) -> dict:
    """
    Get detailed information about a specific workflow execution.

    Use list_workflow_executions first to find execution IDs if needed.

    Args:
        execution_id: The execution ID (e.g., 'stf_datataking-wenauseic-0042')

    Returns: execution_id, workflow_name, namespace, status, executed_by,
    start_time, end_time, parameter_values, performance_metrics
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        try:
            e = WorkflowExecution.objects.select_related('workflow_definition').get(
                execution_id=execution_id
            )
            return {
                "execution_id": e.execution_id,
                "workflow_name": e.workflow_definition.workflow_name if e.workflow_definition else None,
                "namespace": e.namespace,
                "status": e.status,
                "executed_by": e.executed_by,
                "start_time": e.start_time.isoformat() if e.start_time else None,
                "end_time": e.end_time.isoformat() if e.end_time else None,
                "parameter_values": e.parameter_values,
                "performance_metrics": e.performance_metrics,
                "monitor_urls": [
                    {"title": "Execution Detail", "url": _monitor_url(f"/executions/{e.execution_id}/")},
                ],
            }
        except WorkflowExecution.DoesNotExist:
            return {"error": f"Execution '{execution_id}' not found. Use list_workflow_executions to see recent runs."}

    return await fetch()


# -----------------------------------------------------------------------------
# Messages
# -----------------------------------------------------------------------------

@mcp.tool()
async def list_messages(
    namespace: str = None,
    execution_id: str = None,
    agent: str = None,
    message_type: str = None,
    start_time: str = None,
    end_time: str = None,
) -> list:
    """
    List workflow messages with filtering.

    Messages are sent between agents during workflow execution. They record
    events like STF creation, processing completion, state transitions, etc.

    DIAGNOSTIC USE CASES:
    - Track workflow progress: list_messages(execution_id='stf_datataking-user-0044')
    - See what an agent sent: list_messages(agent='daq_simulator-agent-user-123')
    - Debug message flow: list_messages(namespace='torre1', start_time='2025-01-13T11:00:00')
    - For workflow failures: use list_logs(level='ERROR') instead

    Common message types: run_imminent, start_run, stf_gen, end_run, pause_run, resume_run

    Args:
        namespace: Filter to messages from this namespace
        execution_id: Filter to messages for this workflow execution
        agent: Filter to messages from this sender agent
        message_type: Filter by type (e.g., 'stf_gen', 'start_run')
        start_time: Filter messages sent >= this ISO datetime (default: last 1 hour)
        end_time: Filter messages sent <= this ISO datetime

    Returns list of messages (max 200) with: message_type, sender_agent, namespace,
    sent_at, execution_id, run_id, payload_summary
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        qs = WorkflowMessage.objects.order_by('-sent_at')

        if namespace:
            qs = qs.filter(namespace=namespace)
        if execution_id:
            qs = qs.filter(execution_id=execution_id)
        if agent:
            qs = qs.filter(sender_agent=agent)
        if message_type:
            qs = qs.filter(message_type=message_type)

        # Date range filter (default: last hour)
        start = _parse_time(start_time) or _default_start_time(1)
        end = _parse_time(end_time)
        qs = qs.filter(sent_at__gte=start)
        if end:
            qs = qs.filter(sent_at__lte=end)

        # Build URL with filters
        params = []
        if namespace:
            params.append(f"namespace={namespace}")
        if execution_id:
            params.append(f"execution_id={execution_id}")
        if message_type:
            params.append(f"message_type={message_type}")
        query_string = "&".join(params)
        url = _monitor_url(f"/messages/?{query_string}" if query_string else "/messages/")

        return {
            "items": [
                {
                    "message_type": m.message_type,
                    "sender_agent": m.sender_agent,
                    "namespace": m.namespace,
                    "sent_at": m.sent_at.isoformat() if m.sent_at else None,
                    "execution_id": m.execution_id,
                    "run_id": m.run_id,
                    "payload_summary": str(m.message_content)[:200] if m.message_content else None,
                }
                for m in qs[:200]
            ],
            "monitor_urls": [
                {"title": "Messages List", "url": url},
            ],
        }

    return await fetch()


# -----------------------------------------------------------------------------
# Runs
# -----------------------------------------------------------------------------

@mcp.tool()
async def list_runs(
    start_time: str = None,
    end_time: str = None,
) -> list:
    """
    List simulation runs with timing and file counts.

    Runs represent data-taking periods in the ePIC detector system.
    Each run contains multiple STF (Super Time Frame) files.

    Args:
        start_time: Filter runs started >= this ISO datetime (default: last 7 days)
        end_time: Filter runs started <= this ISO datetime

    Returns list of runs with: run_number, start_time, end_time, duration_seconds,
    stf_file_count
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        qs = Run.objects.annotate(
            stf_file_count=Count('stf_files')
        ).order_by('-start_time')

        # Date range filter (default: last 7 days)
        start = _parse_time(start_time) or _default_start_time(168)
        end = _parse_time(end_time)
        qs = qs.filter(start_time__gte=start)
        if end:
            qs = qs.filter(start_time__lte=end)

        items = []
        for r in qs[:100]:
            duration = None
            if r.start_time and r.end_time:
                duration = (r.end_time - r.start_time).total_seconds()

            items.append({
                "run_number": r.run_number,
                "start_time": r.start_time.isoformat() if r.start_time else None,
                "end_time": r.end_time.isoformat() if r.end_time else None,
                "duration_seconds": duration,
                "stf_file_count": r.stf_file_count,
            })

        return {
            "items": items,
            "monitor_urls": [
                {"title": "Runs List", "url": _monitor_url("/runs/")},
            ],
        }

    return await fetch()


@mcp.tool()
async def get_run(run_number: int) -> dict:
    """
    Get detailed information about a specific run.

    Args:
        run_number: The run number (required)

    Returns: run_number, start_time, end_time, duration_seconds, run_conditions,
    file_stats (counts by status)
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        try:
            r = Run.objects.get(run_number=run_number)

            # Calculate duration
            duration = None
            if r.start_time and r.end_time:
                duration = (r.end_time - r.start_time).total_seconds()

            # Get file stats by status
            file_stats = {}
            stf_files = StfFile.objects.filter(run=r)
            for status_choice in StfFile._meta.get_field('status').choices:
                status_value = status_choice[0]
                file_stats[status_value] = stf_files.filter(status=status_value).count()

            return {
                "run_number": r.run_number,
                "start_time": r.start_time.isoformat() if r.start_time else None,
                "end_time": r.end_time.isoformat() if r.end_time else None,
                "duration_seconds": duration,
                "run_conditions": r.run_conditions,
                "file_stats": file_stats,
                "total_stf_files": sum(file_stats.values()),
                "monitor_urls": [
                    {"title": "Run Detail", "url": _monitor_url(f"/runs/{r.run_number}/")},
                ],
            }
        except Run.DoesNotExist:
            return {"error": f"Run {run_number} not found. Use list_runs to see available runs."}

    return await fetch()


# -----------------------------------------------------------------------------
# STF Files
# -----------------------------------------------------------------------------

@mcp.tool()
async def list_stf_files(
    run_number: int = None,
    status: str = None,
    machine_state: str = None,
    start_time: str = None,
    end_time: str = None,
) -> list:
    """
    List STF (Super Time Frame) files with filtering.

    STF files are the primary data units from the ePIC detector DAQ system.
    Each STF represents a time slice of detector data.

    Args:
        run_number: Filter to files from this run
        status: Filter by status: 'registered', 'processing', 'processed', 'done', 'failed'
        machine_state: Filter by detector state (e.g., 'physics', 'cosmics')
        start_time: Filter files created >= this ISO datetime (default: last 24 hours)
        end_time: Filter files created <= this ISO datetime

    Returns list of STF files with: file_id, stf_filename, run_number, status,
    machine_state, file_size_bytes, created_at, tf_file_count
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        qs = StfFile.objects.select_related('run').annotate(
            tf_file_count=Count('tf_files')
        ).order_by('-created_at')

        if run_number:
            qs = qs.filter(run__run_number=run_number)
        if status:
            qs = qs.filter(status__iexact=status)
        if machine_state:
            qs = qs.filter(machine_state__iexact=machine_state)

        # Date range filter (skip default if specific run is requested)
        start = _parse_time(start_time) or (None if run_number else _default_start_time(24))
        end = _parse_time(end_time)
        if start:
            qs = qs.filter(created_at__gte=start)
        if end:
            qs = qs.filter(created_at__lte=end)

        # Build URL with filters
        params = []
        if run_number:
            params.append(f"run_number={run_number}")
        if status:
            params.append(f"status={status}")
        query_string = "&".join(params)
        url = _monitor_url(f"/stf-files/?{query_string}" if query_string else "/stf-files/")

        return {
            "items": [
                {
                    "file_id": str(f.file_id),
                    "stf_filename": f.stf_filename,
                    "run_number": f.run.run_number if f.run else None,
                    "status": f.status,
                    "machine_state": f.machine_state,
                    "file_size_bytes": f.file_size_bytes,
                    "created_at": f.created_at.isoformat() if f.created_at else None,
                    "tf_file_count": f.tf_file_count,
                }
                for f in qs[:100]
            ],
            "monitor_urls": [
                {"title": "STF Files List", "url": url},
            ],
        }

    return await fetch()


@mcp.tool()
async def get_stf_file(file_id: str = None, stf_filename: str = None) -> dict:
    """
    Get detailed information about a specific STF file.

    Provide either file_id or stf_filename to identify the file.

    Args:
        file_id: The UUID file ID
        stf_filename: The STF filename

    Returns: file_id, stf_filename, run_number, status, machine_state,
    file_size_bytes, checksum, created_at, metadata, workflow_id, daq_state,
    daq_substate, workflow_status
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        try:
            if file_id:
                f = StfFile.objects.select_related('run').get(file_id=file_id)
            elif stf_filename:
                f = StfFile.objects.select_related('run').get(stf_filename=stf_filename)
            else:
                return {"error": "Provide either file_id or stf_filename"}

            return {
                "file_id": str(f.file_id),
                "stf_filename": f.stf_filename,
                "run_number": f.run.run_number if f.run else None,
                "status": f.status,
                "machine_state": f.machine_state,
                "file_size_bytes": f.file_size_bytes,
                "checksum": f.checksum,
                "created_at": f.created_at.isoformat() if f.created_at else None,
                "metadata": f.metadata,
                "workflow_id": str(f.workflow_id) if f.workflow_id else None,
                "daq_state": f.daq_state,
                "daq_substate": f.daq_substate,
                "workflow_status": f.workflow_status,
                "monitor_urls": [
                    {"title": "STF File Detail", "url": _monitor_url(f"/stf-files/{f.file_id}/")},
                ],
            }
        except StfFile.DoesNotExist:
            return {"error": "STF file not found. Use list_stf_files to see available files."}

    return await fetch()


# -----------------------------------------------------------------------------
# TF Slices (Fast Processing)
# -----------------------------------------------------------------------------

@mcp.tool()
async def list_tf_slices(
    run_number: int = None,
    stf_filename: str = None,
    tf_filename: str = None,
    status: str = None,
    assigned_worker: str = None,
    start_time: str = None,
    end_time: str = None,
) -> list:
    """
    List TF slices for the fast processing workflow.

    TF slices are small portions of TF samples (~15 per STF) that workers
    process independently in ~30 seconds each.

    Args:
        run_number: Filter to slices from this run
        stf_filename: Filter to slices from this STF file
        tf_filename: Filter to slices from this TF sample
        status: Filter by status: 'queued', 'processing', 'completed', 'failed'
        assigned_worker: Filter by assigned worker ID
        start_time: Filter slices created >= this ISO datetime (default: last 24 hours)
        end_time: Filter slices created <= this ISO datetime

    Returns list of slices with: slice_id, tf_filename, stf_filename, run_number,
    tf_first, tf_last, tf_count, status, assigned_worker, created_at, completed_at
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        qs = TFSlice.objects.all().order_by('-created_at')

        if run_number:
            qs = qs.filter(run_number=run_number)
        if stf_filename:
            qs = qs.filter(stf_filename=stf_filename)
        if tf_filename:
            qs = qs.filter(tf_filename=tf_filename)
        if status:
            qs = qs.filter(status__iexact=status)
        if assigned_worker:
            qs = qs.filter(assigned_worker=assigned_worker)

        # Date range filter (skip default if specific context is requested)
        has_context = run_number or stf_filename or tf_filename
        start = _parse_time(start_time) or (None if has_context else _default_start_time(24))
        end = _parse_time(end_time)
        if start:
            qs = qs.filter(created_at__gte=start)
        if end:
            qs = qs.filter(created_at__lte=end)

        # Build URL with filters
        params = []
        if run_number:
            params.append(f"run_number={run_number}")
        if status:
            params.append(f"status={status}")
        query_string = "&".join(params)
        url = _monitor_url(f"/tf-slices/?{query_string}" if query_string else "/tf-slices/")

        return {
            "items": [
                {
                    "slice_id": s.slice_id,
                    "tf_filename": s.tf_filename,
                    "stf_filename": s.stf_filename,
                    "run_number": s.run_number,
                    "tf_first": s.tf_first,
                    "tf_last": s.tf_last,
                    "tf_count": s.tf_count,
                    "status": s.status,
                    "assigned_worker": s.assigned_worker,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                    "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                }
                for s in qs[:200]
            ],
            "monitor_urls": [
                {"title": "TF Slices List", "url": url},
            ],
        }

    return await fetch()


@mcp.tool()
async def get_tf_slice(tf_filename: str, slice_id: int) -> dict:
    """
    Get detailed information about a specific TF slice.

    Args:
        tf_filename: The TF filename (required)
        slice_id: The slice ID within the TF (required, typically 1-15)

    Returns: slice_id, tf_filename, stf_filename, run_number, tf_first, tf_last,
    tf_count, status, retries, assigned_worker, assigned_at, completed_at, metadata
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        try:
            s = TFSlice.objects.get(tf_filename=tf_filename, slice_id=slice_id)
            return {
                "slice_id": s.slice_id,
                "tf_filename": s.tf_filename,
                "stf_filename": s.stf_filename,
                "run_number": s.run_number,
                "tf_first": s.tf_first,
                "tf_last": s.tf_last,
                "tf_count": s.tf_count,
                "status": s.status,
                "retries": s.retries,
                "assigned_worker": s.assigned_worker,
                "assigned_at": s.assigned_at.isoformat() if s.assigned_at else None,
                "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                "metadata": s.metadata,
            }
        except TFSlice.DoesNotExist:
            return {"error": f"TF slice {slice_id} for {tf_filename} not found. Use list_tf_slices to see available slices."}

    return await fetch()


# -----------------------------------------------------------------------------
# Logs
# -----------------------------------------------------------------------------

@mcp.tool()
async def list_logs(
    app_name: str = None,
    instance_name: str = None,
    execution_id: str = None,
    level: str = None,
    search: str = None,
    start_time: str = None,
    end_time: str = None,
) -> list:
    """
    List application log entries with filtering.

    All agents log to the central database via Python's logging module.
    Use this tool to discover errors, debug issues, and understand system behavior.

    DIAGNOSTIC USE CASES:
    - Workflow logs: list_logs(execution_id='stf_datataking-user-0044')
    - Debug a specific agent: list_logs(instance_name='daq_simulator-agent-user-123')
    - Find all errors: list_logs(level='ERROR')
    - Search for specific issues: list_logs(search='connection failed')

    Args:
        app_name: Filter by application name (e.g., 'daq_simulator', 'data_agent')
        instance_name: Filter by agent instance name
        execution_id: Filter by workflow execution ID (e.g., 'stf_datataking-wenauseic-0044')
        level: Minimum log level - returns this level and higher severity.
               DEBUG (all), INFO, WARNING, ERROR, CRITICAL
        search: Case-insensitive text search in log message
        start_time: Filter logs from this ISO datetime (default: last 24 hours)
        end_time: Filter logs until this ISO datetime

    Returns list of log entries (max 200) with: id, timestamp, app_name, instance_name,
    level, message, module, funcname, lineno
    """
    import logging as py_logging
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        qs = AppLog.objects.all().order_by('-timestamp')

        if app_name:
            qs = qs.filter(app_name=app_name)
        if instance_name:
            qs = qs.filter(instance_name=instance_name)
        if execution_id:
            qs = qs.filter(extra_data__execution_id=execution_id)

        # Level filtering (threshold - specified level and above)
        if level:
            level_map = {
                'DEBUG': py_logging.DEBUG,
                'INFO': py_logging.INFO,
                'WARNING': py_logging.WARNING,
                'ERROR': py_logging.ERROR,
                'CRITICAL': py_logging.CRITICAL,
            }
            if level.upper() in level_map:
                qs = qs.filter(level__gte=level_map[level.upper()])

        # Case-insensitive text search in message
        if search:
            qs = qs.filter(message__icontains=search)

        # Date range filter (default: last 24 hours)
        start = _parse_time(start_time) or _default_start_time(24)
        end = _parse_time(end_time)
        qs = qs.filter(timestamp__gte=start)
        if end:
            qs = qs.filter(timestamp__lte=end)

        # Build URL with filters
        params = []
        if instance_name:
            params.append(f"instance_name={instance_name}")
        if execution_id:
            params.append(f"execution_id={execution_id}")
        if level:
            params.append(f"level={level}")
        query_string = "&".join(params)
        url = _monitor_url(f"/logs/?{query_string}" if query_string else "/logs/")

        return {
            "items": [
                {
                    "id": log.id,
                    "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                    "app_name": log.app_name,
                    "instance_name": log.instance_name,
                    "level": log.levelname,
                    "message": log.message,
                    "module": log.module,
                    "funcname": log.funcname,
                    "lineno": log.lineno,
                    "extra_data": log.extra_data,
                }
                for log in qs[:200]
            ],
            "monitor_urls": [
                {"title": "Logs List", "url": url},
            ],
        }

    return await fetch()


@mcp.tool()
async def get_log_entry(log_id: int) -> dict:
    """
    Get full details of a specific log entry.

    Args:
        log_id: The log entry ID (from list_logs)

    Returns: Full log entry with all fields including extra_data
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        try:
            log = AppLog.objects.get(id=log_id)
            return {
                "id": log.id,
                "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                "app_name": log.app_name,
                "instance_name": log.instance_name,
                "level": log.levelname,
                "message": log.message,
                "module": log.module,
                "funcname": log.funcname,
                "lineno": log.lineno,
                "process": log.process,
                "thread": log.thread,
                "extra_data": log.extra_data,
                "monitor_urls": [
                    {"title": "Log Detail", "url": _monitor_url(f"/logs/{log.id}/")},
                ],
            }
        except AppLog.DoesNotExist:
            return {"error": f"Log entry {log_id} not found."}

    return await fetch()


# -----------------------------------------------------------------------------
# Action Tools
# -----------------------------------------------------------------------------

@mcp.tool()
async def start_workflow(
    workflow_name: str = None,
    namespace: str = None,
    config: str = None,
    realtime: bool = None,
    duration: int = 0,
    stf_count: int = None,
    physics_period_count: int = None,
    physics_period_duration: float = None,
    stf_interval: float = None,
) -> dict:
    """
    Start a workflow execution by sending a command to the DAQ Simulator agent.

    All parameters are optional - defaults are read from PersistentState 'workflow_defaults'.
    Call with no arguments to use configured defaults.

    Args:
        workflow_name: Name of the workflow (default: from config, typically 'stf_datataking')
        namespace: Testbed namespace (default: from config, e.g., 'torre1')
        config: Workflow config name (default: from config, e.g., 'fast_processing_default')
        realtime: Run in real-time mode (default: from config, typically True)
        duration: Max duration in seconds (0 = run until complete)
        stf_count: Number of STF files to generate (overrides config)
        physics_period_count: Number of physics periods (overrides config)
        physics_period_duration: Duration of each physics period in seconds (overrides config)
        stf_interval: Interval between STF generation in seconds (overrides config)

    Returns:
        Success/failure status with execution_id if started.

    After starting, monitor with:
        get_workflow_execution(execution_id) → status: running/completed/failed/terminated
        list_messages(execution_id='...') → progress events
        list_logs(execution_id='...') → workflow logs including errors
    """
    import json
    from datetime import datetime
    from asgiref.sync import sync_to_async

    @sync_to_async
    def do_start():
        import os
        from pathlib import Path
        from .activemq_connection import ActiveMQConnectionManager

        # Read defaults from testbed config (respects SWF_TESTBED_CONFIG env var)
        toml_namespace = None
        toml_workflow_name = None
        toml_config = None
        toml_realtime = None
        toml_params = {}

        testbed_toml, config_source = _get_testbed_config_path()
        if testbed_toml.exists():
            try:
                import tomllib
                with open(testbed_toml, 'rb') as f:
                    toml_data = tomllib.load(f)
                toml_namespace = toml_data.get('testbed', {}).get('namespace')
                workflow_section = toml_data.get('workflow', {})
                toml_workflow_name = workflow_section.get('name')
                toml_config = workflow_section.get('config')
                toml_realtime = workflow_section.get('realtime')
                # Get ALL parameters from [parameters] section - no hardcoding
                toml_params = toml_data.get('parameters', {})
                if config_source == 'SWF_TESTBED_CONFIG':
                    logger.info(f"Using config from SWF_TESTBED_CONFIG: {testbed_toml.name}")
            except Exception as e:
                logger.warning(f"Failed to read {testbed_toml}: {e}")

        # Apply defaults - explicit MCP args override toml values
        actual_workflow_name = workflow_name or toml_workflow_name or 'stf_datataking'
        actual_namespace = namespace or toml_namespace or 'torre1'
        actual_config = config or toml_config or 'fast_processing_default'
        actual_realtime = realtime if realtime is not None else (toml_realtime if toml_realtime is not None else True)

        # Build params - start with ALL toml [parameters], then override with explicit MCP args
        params = dict(toml_params)
        if stf_count is not None:
            params['stf_count'] = stf_count
        if physics_period_count is not None:
            params['physics_period_count'] = physics_period_count
        if physics_period_duration is not None:
            params['physics_period_duration'] = physics_period_duration
        if stf_interval is not None:
            params['stf_interval'] = stf_interval

        # Include namespace in params so it flows through to execution record
        params['namespace'] = actual_namespace

        # Build message (namespace at root for clarity, in params for override flow)
        msg = {
            'msg_type': 'run_workflow',
            'namespace': actual_namespace,
            'workflow_name': actual_workflow_name,
            'config': actual_config,
            'realtime': actual_realtime,
            'duration': duration,
            'params': params,
            'timestamp': datetime.now().isoformat(),
            'source': 'mcp'
        }

        # Send to workflow_control queue
        mq = ActiveMQConnectionManager()
        if mq.send_message('/queue/workflow_control', json.dumps(msg)):
            logger.info(
                f"MCP start_workflow: sent run_workflow command for '{actual_workflow_name}' "
                f"(namespace={actual_namespace}, config={actual_config}, realtime={actual_realtime})"
            )
            return {
                "success": True,
                "message": f"Workflow '{actual_workflow_name}' start command sent to DAQ Simulator",
                "workflow_name": actual_workflow_name,
                "namespace": actual_namespace,
                "config": actual_config,
                "realtime": actual_realtime,
                "params": params,
                "note": "Workflow runs asynchronously. Use list_workflow_executions to monitor."
            }
        else:
            return {
                "success": False,
                "error": "Failed to send message to ActiveMQ. Is the message broker running?",
                "workflow_name": actual_workflow_name,
                "namespace": actual_namespace,
            }

    return await do_start()


@mcp.tool()
async def stop_workflow(execution_id: str) -> dict:
    """
    Stop a running workflow by sending a stop command to the DAQ Simulator agent.

    Sends a stop_workflow command that the agent checks between simulation events.
    The workflow stops gracefully at the next checkpoint.

    To find the execution_id, use list_workflow_executions(currently_running=True).

    Args:
        execution_id: The execution ID to stop (e.g., 'stf_datataking-wenauseic-0042')

    Returns:
        Success/failure status. The actual stop is asynchronous - monitor via
        list_workflow_executions to confirm termination.
    """
    import json
    from datetime import datetime
    from asgiref.sync import sync_to_async

    @sync_to_async
    def do_stop():
        from .activemq_connection import ActiveMQConnectionManager

        # Get namespace from execution record for message routing
        try:
            execution = WorkflowExecution.objects.get(execution_id=execution_id)
        except WorkflowExecution.DoesNotExist:
            return {
                "success": False,
                "error": f"Execution '{execution_id}' not found",
            }

        if execution.status != 'running':
            return {
                "success": False,
                "error": f"Execution '{execution_id}' is not running (status: {execution.status})",
            }

        msg = {
            'msg_type': 'stop_workflow',
            'execution_id': execution_id,
            'namespace': execution.namespace,
            'timestamp': datetime.now().isoformat(),
            'source': 'mcp'
        }

        mq = ActiveMQConnectionManager()
        if mq.send_message('/queue/workflow_control', json.dumps(msg)):
            logger.info(f"MCP stop_workflow: sent stop command for execution '{execution_id}'")
            return {
                "success": True,
                "message": f"Stop command sent for execution '{execution_id}'",
                "execution_id": execution_id,
                "namespace": execution.namespace,
                "note": "Workflow will stop at next checkpoint. Monitor via list_workflow_executions."
            }
        else:
            return {
                "success": False,
                "error": "Failed to send message to ActiveMQ. Is the message broker running?",
                "execution_id": execution_id,
            }

    return await do_stop()


@mcp.tool()
async def end_execution(execution_id: str) -> dict:
    """
    End a running workflow execution by setting its status to 'terminated'.

    Use this to clean up stale or stuck executions that are still marked as 'running'.
    This is a state change only - no data is deleted. The action is logged.

    Args:
        execution_id: The execution ID to end (use list_workflow_executions to find running ones)

    Returns:
        Success/failure status with details
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def do_end():
        try:
            execution = WorkflowExecution.objects.get(execution_id=execution_id)
        except WorkflowExecution.DoesNotExist:
            return {
                "success": False,
                "error": f"Execution '{execution_id}' not found",
            }

        old_status = execution.status
        if old_status != 'running':
            return {
                "success": False,
                "error": f"Execution '{execution_id}' is not running (status: {old_status})",
            }

        execution.status = 'terminated'
        execution.end_time = timezone.now()
        execution.save()

        logger.info(
            f"MCP end_execution: '{execution_id}' terminated (was running since {execution.start_time})"
        )

        return {
            "success": True,
            "execution_id": execution_id,
            "old_status": old_status,
            "new_status": "terminated",
            "start_time": execution.start_time.isoformat() if execution.start_time else None,
            "end_time": execution.end_time.isoformat() if execution.end_time else None,
        }

    return await do_end()


@mcp.tool()
async def kill_agent(name: str) -> dict:
    """
    Kill an agent process by sending SIGKILL to its PID.

    Looks up the agent by instance_name, retrieves its pid and hostname,
    and sends SIGKILL if the agent is on the current host. Sets the agent's
    status and operational_state to EXITED, so it won't appear in default
    list_agents results.

    Args:
        name: The exact agent instance name (e.g., 'daq_simulator-agent-wenauseic-308')

    Returns:
        Success/failure status with details
    """
    import os
    import signal
    import socket
    from asgiref.sync import sync_to_async

    current_host = socket.gethostname()

    @sync_to_async
    def do_kill():
        try:
            agent = SystemAgent.objects.get(instance_name=name)
        except SystemAgent.DoesNotExist:
            return {
                "success": False,
                "error": f"Agent '{name}' not found. Use list_agents to see available agents.",
            }

        pid = agent.pid
        hostname = agent.hostname
        old_state = agent.operational_state
        killed = False
        kill_error = None

        # Try to kill if we have a PID and it's on this host
        if pid:
            if hostname and hostname != current_host:
                kill_error = f"Agent on '{hostname}', not '{current_host}' - cannot kill remotely"
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed = True
                except ProcessLookupError:
                    kill_error = f"Process {pid} not found (already dead)"
                except PermissionError:
                    kill_error = f"Permission denied to kill process {pid}"
                except Exception as e:
                    kill_error = str(e)

        # Always mark EXITED
        agent.operational_state = 'EXITED'
        agent.status = 'EXITED'
        agent.save(update_fields=['operational_state', 'status'])

        logger.info(f"MCP kill_agent: '{name}' pid={pid} killed={killed} error={kill_error}")

        return {
            "success": True,
            "name": name,
            "pid": pid,
            "hostname": hostname,
            "killed": killed,
            "kill_error": kill_error,
            "old_state": old_state,
            "new_state": "EXITED",
        }

    return await do_kill()


# -----------------------------------------------------------------------------
# User Agent Manager Tools
# -----------------------------------------------------------------------------

@mcp.tool()
async def check_agent_manager(username: str = None) -> dict:
    """
    Check if a user's agent manager daemon is alive.

    The agent manager is a lightweight per-user daemon that listens for MCP commands
    to control testbed agents. It sends periodic heartbeats to the monitor.

    Args:
        username: The username to check. If not provided, uses current user.

    Returns:
        - alive: True if agent manager has recent heartbeat (within 5 minutes)
        - instance_name: The agent manager's instance name
        - namespace: Current testbed namespace (from config)
        - last_heartbeat: When it last checked in
        - control_queue: The queue to send commands to
        - agents_running: Whether testbed agents are running
        - how_to_start: Instructions if not alive
    """
    import getpass
    from asgiref.sync import sync_to_async

    if not username:
        username = getpass.getuser()

    instance_name = f'agent-manager-{username}'
    control_queue = f'/queue/agent_control.{username}'

    @sync_to_async
    def fetch():
        try:
            agent = SystemAgent.objects.get(instance_name=instance_name)
            now = timezone.now()
            recent_threshold = now - timedelta(minutes=5)

            alive = (
                agent.last_heartbeat is not None and
                agent.last_heartbeat >= recent_threshold and
                agent.operational_state != 'EXITED'
            )

            metadata = agent.metadata or {}

            return {
                "alive": alive,
                "username": username,
                "instance_name": instance_name,
                "namespace": agent.namespace,
                "last_heartbeat": agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
                "operational_state": agent.operational_state,
                "control_queue": control_queue,
                "agents_running": metadata.get('agents_running', False),
            }
        except SystemAgent.DoesNotExist:
            return {
                "alive": False,
                "username": username,
                "instance_name": instance_name,
                "namespace": None,
                "last_heartbeat": None,
                "operational_state": None,
                "control_queue": control_queue,
                "agents_running": False,
                "how_to_start": f"Run 'testbed agent-manager' in {username}'s swf-testbed directory",
            }

    return await fetch()


@mcp.tool()
async def start_user_testbed(username: str = None, config_name: str = "testbed.toml") -> dict:
    """
    Start a user's testbed via their agent manager daemon.

    Sends a start_testbed command to the user's agent manager, which then
    starts supervisord-managed agents. The agent manager must be running first.

    Args:
        username: The username whose testbed to start. If not provided, uses current user.
        config_name: Config file name in workflows/ directory (default: testbed.toml).

    Returns:
        Success/failure status. If agent manager is not running, provides instructions.
    """
    import json
    import getpass
    from datetime import datetime
    from asgiref.sync import sync_to_async

    if not username:
        username = getpass.getuser()

    # First check if agent manager is alive
    manager_status = await check_agent_manager(username)
    if not manager_status.get('alive'):
        return {
            "success": False,
            "error": f"Agent manager for '{username}' is not running",
            "how_to_start": f"Run 'testbed agent-manager' in {username}'s swf-testbed directory",
            "username": username,
        }

    control_queue = f'/queue/agent_control.{username}'
    instance_name = f'agent-manager-{username}'

    @sync_to_async
    def send_commands():
        import time
        from .activemq_connection import ActiveMQConnectionManager
        mq = ActiveMQConnectionManager()

        # Record the old PID before restart
        old_pid = None
        try:
            old_agent = SystemAgent.objects.get(instance_name=instance_name)
            old_pid = old_agent.pid
            old_heartbeat = old_agent.last_heartbeat
        except SystemAgent.DoesNotExist:
            old_heartbeat = None

        # First restart the agent manager to pick up fresh code
        restart_msg = {
            'command': 'restart',
            'timestamp': datetime.now().isoformat(),
            'source': 'mcp'
        }
        if not mq.send_message(control_queue, json.dumps(restart_msg)):
            return {
                "success": False,
                "error": "Failed to send restart command to ActiveMQ",
                "username": username,
            }

        logger.info(f"MCP start_user_testbed: sent restart command, waiting for new agent manager")

        # Poll for new agent manager to be ready (new PID or fresh heartbeat)
        max_wait = 15  # seconds
        poll_interval = 1
        waited = 0
        new_agent_ready = False

        while waited < max_wait:
            time.sleep(poll_interval)
            waited += poll_interval
            try:
                agent = SystemAgent.objects.get(instance_name=instance_name)
                # Check if this is a new agent (different PID or fresh heartbeat)
                now = timezone.now()
                is_new = (
                    (old_pid and agent.pid and agent.pid != old_pid) or
                    (agent.last_heartbeat and old_heartbeat and agent.last_heartbeat > old_heartbeat)
                )
                is_healthy = (
                    agent.operational_state == 'READY' and
                    agent.last_heartbeat and
                    (now - agent.last_heartbeat).total_seconds() < 60
                )
                if is_new and is_healthy:
                    new_agent_ready = True
                    logger.info(f"MCP start_user_testbed: new agent manager ready (pid={agent.pid})")
                    break
            except SystemAgent.DoesNotExist:
                pass

        if not new_agent_ready:
            logger.warning(f"MCP start_user_testbed: agent manager not confirmed ready after {max_wait}s")

        # Now send start_testbed command
        start_msg = {
            'command': 'start_testbed',
            'config_name': config_name,
            'timestamp': datetime.now().isoformat(),
            'source': 'mcp'
        }

        if mq.send_message(control_queue, json.dumps(start_msg)):
            logger.info(
                f"MCP start_user_testbed: sent start_testbed command for user '{username}' "
                f"(config={config_name})"
            )
            return {
                "success": True,
                "message": f"Agent manager restarted and start command sent",
                "username": username,
                "config_name": config_name,
                "control_queue": control_queue,
                "new_agent_ready": new_agent_ready,
                "note": "Agents will start asynchronously. Use list_agents to verify.",
            }
        else:
            return {
                "success": False,
                "error": "Failed to send start_testbed command to ActiveMQ",
                "username": username,
            }

    return await send_commands()


@mcp.tool()
async def stop_user_testbed(username: str = None) -> dict:
    """
    Stop a user's testbed via their agent manager daemon.

    Sends a stop_testbed command to the user's agent manager, which then
    stops all supervisord-managed agents.

    Args:
        username: The username whose testbed to stop. If not provided, uses current user.

    Returns:
        Success/failure status.
    """
    import json
    import getpass
    from datetime import datetime
    from asgiref.sync import sync_to_async

    if not username:
        username = getpass.getuser()

    # First check if agent manager is alive
    manager_status = await check_agent_manager(username)
    if not manager_status.get('alive'):
        return {
            "success": False,
            "error": f"Agent manager for '{username}' is not running",
            "username": username,
            "note": "If agents are still running, you can kill them directly with kill_agent()",
        }

    control_queue = f'/queue/agent_control.{username}'

    @sync_to_async
    def send_command():
        from .activemq_connection import ActiveMQConnectionManager

        msg = {
            'command': 'stop_testbed',
            'timestamp': datetime.now().isoformat(),
            'source': 'mcp'
        }

        mq = ActiveMQConnectionManager()
        if mq.send_message(control_queue, json.dumps(msg)):
            logger.info(f"MCP stop_user_testbed: sent stop_testbed command for user '{username}'")
            return {
                "success": True,
                "message": f"Stop command sent to {username}'s agent manager",
                "username": username,
                "control_queue": control_queue,
                "note": "Agents will stop asynchronously. Use list_agents to verify.",
            }
        else:
            return {
                "success": False,
                "error": "Failed to send message to ActiveMQ. Is the message broker running?",
                "username": username,
            }

    return await send_command()


@mcp.tool()
async def get_testbed_status(username: str = None) -> dict:
    """
    Get comprehensive status of a user's testbed.

    Shows agent manager status, namespace, and all workflow agents with their
    current state (running/stopped based on heartbeat freshness).

    Args:
        username: The username to check. If not provided, uses current user.

    Returns:
        - agent_manager: Status of the agent manager daemon
        - namespace: Current testbed namespace
        - agents: List of workflow agents with status
        - summary: Quick counts of running/stopped agents
    """
    import getpass
    from asgiref.sync import sync_to_async

    if not username:
        username = getpass.getuser()

    # Get agent manager status first
    manager_status = await check_agent_manager(username)
    namespace = manager_status.get('namespace')

    @sync_to_async
    def fetch_agents():
        now = timezone.now()
        healthy_threshold = now - timedelta(minutes=2)

        # Get agents in the namespace (if we have one)
        agents_info = []
        running_count = 0
        stopped_count = 0

        if namespace:
            agents = SystemAgent.objects.filter(
                namespace=namespace
            ).exclude(
                agent_type='agent_manager'
            ).exclude(
                operational_state='EXITED'
            ).order_by('-last_heartbeat')

            for agent in agents:
                is_running = (
                    agent.last_heartbeat and
                    agent.last_heartbeat >= healthy_threshold
                )
                if is_running:
                    running_count += 1
                else:
                    stopped_count += 1

                agents_info.append({
                    'name': agent.instance_name,
                    'type': agent.agent_type,
                    'status': 'running' if is_running else 'stopped',
                    'last_heartbeat': agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
                })

        return {
            'agents': agents_info,
            'running': running_count,
            'stopped': stopped_count,
        }

    agents_data = await fetch_agents()

    return {
        'username': username,
        'agent_manager': {
            'alive': manager_status.get('alive'),
            'namespace': namespace,
            'operational_state': manager_status.get('operational_state'),
            'last_heartbeat': manager_status.get('last_heartbeat'),
        },
        'agents': agents_data['agents'],
        'summary': {
            'running': agents_data['running'],
            'stopped': agents_data['stopped'],
        },
        'ready': manager_status.get('alive', False) and agents_data['running'] == 0,
        'note': 'ready=True means testbed is idle and ready to start' if agents_data['running'] == 0 else None,
    }


# -----------------------------------------------------------------------------
# Workflow Monitoring
# -----------------------------------------------------------------------------

@mcp.tool()
async def get_workflow_monitor(execution_id: str) -> dict:
    """
    Get the status and accumulated events for a workflow execution.

    This provides a summary of workflow progress without needing to poll
    multiple tools. Aggregates messages and logs for the execution.

    Args:
        execution_id: The execution ID to get monitor status for

    Returns:
        - execution_id: The execution being monitored
        - status: Current workflow status (running/completed/failed/terminated)
        - phase: Current phase (imminent/running/ended)
        - events: List of key events with timestamps
        - stf_count: Number of STF files generated
        - errors: List of any errors encountered
        - duration_seconds: How long the workflow ran (if completed)
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        # Get current execution status from database
        try:
            execution = WorkflowExecution.objects.get(execution_id=execution_id)
            db_status = execution.status
            db_start_time = execution.start_time
            db_end_time = execution.end_time
        except WorkflowExecution.DoesNotExist:
            return {"error": f"Execution '{execution_id}' not found"}

        # Calculate duration
        duration_seconds = None
        if db_start_time and db_end_time:
            duration_seconds = (db_end_time - db_start_time).total_seconds()

        # Get messages for this execution
        messages = WorkflowMessage.objects.filter(
            execution_id=execution_id
        ).order_by('sent_at')

        # Accumulate events
        events = []
        stf_count = 0
        current_phase = "unknown"
        run_id = None
        errors = []

        for msg in messages:
            msg_type = msg.message_type
            timestamp = msg.sent_at.isoformat() if msg.sent_at else None
            content = msg.message_content or {}

            if msg_type == 'run_imminent':
                current_phase = "imminent"
                run_id = content.get('run_id') or msg.run_id
                events.append({"type": "run_imminent", "time": timestamp, "run_id": run_id})
            elif msg_type == 'start_run':
                current_phase = "running"
                events.append({"type": "start_run", "time": timestamp})
            elif msg_type == 'stf_gen':
                stf_count += 1
            elif msg_type == 'end_run':
                current_phase = "ended"
                events.append({"type": "end_run", "time": timestamp, "stf_count": stf_count})
            elif msg_type in ('run_workflow_failed', 'error'):
                errors.append({
                    "time": timestamp,
                    "error": content.get('error', str(content)),
                })

        # Check for errors in logs
        import logging as py_logging
        error_logs = AppLog.objects.filter(
            level__gte=py_logging.ERROR,
            extra_data__execution_id=execution_id,
        ).order_by('timestamp')[:10]

        for log in error_logs:
            errors.append({
                "time": log.timestamp.isoformat() if log.timestamp else None,
                "error": log.message,
                "source": "log",
            })

        return {
            "execution_id": execution_id,
            "status": db_status,
            "phase": current_phase,
            "run_id": run_id,
            "stf_count": stf_count,
            "events": events,
            "errors": errors,
            "start_time": db_start_time.isoformat() if db_start_time else None,
            "end_time": db_end_time.isoformat() if db_end_time else None,
            "duration_seconds": duration_seconds,
            "monitor_urls": [
                {"title": "Execution Detail", "url": _monitor_url(f"/executions/{execution_id}/")},
            ],
        }

    return await fetch()


@mcp.tool()
async def list_workflow_monitors() -> list:
    """
    List recent workflow executions that can be monitored.

    Returns executions from the last 24 hours with their current status,
    allowing you to pick one to monitor with get_workflow_monitor().

    Returns list of executions with: execution_id, status, start_time, stf_count
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        now = timezone.now()
        executions = WorkflowExecution.objects.filter(
            start_time__gte=now - timedelta(hours=24)
        ).order_by('-start_time')[:20]

        items = []
        for e in executions:
            # Count STF messages for this execution
            stf_count = WorkflowMessage.objects.filter(
                execution_id=e.execution_id,
                message_type='stf_gen',
            ).count()

            items.append({
                "execution_id": e.execution_id,
                "status": e.status,
                "start_time": e.start_time.isoformat() if e.start_time else None,
                "end_time": e.end_time.isoformat() if e.end_time else None,
                "stf_count": stf_count,
            })

        return {
            "items": items,
            "monitor_urls": [
                {"title": "Executions List", "url": _monitor_url("/executions/")},
            ],
        }

    return await fetch()
