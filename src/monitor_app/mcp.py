"""
MCP Tools for ePIC Streaming Workflow Testbed Monitor.

These tools enable LLM-based natural language interaction with the testbed,
allowing users to query system state, agents, workflows, runs, STF files,
TF slices, and messages.

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
            "description": "Get comprehensive system state: agents, executions, run states, health",
            "parameters": [],
        },
        {
            "name": "list_agents",
            "description": "List registered agents with filtering by namespace, type, status, date range",
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
            "description": "Start a workflow execution (NOT YET IMPLEMENTED)",
            "parameters": ["workflow_name", "namespace"],
        },
        {
            "name": "stop_workflow",
            "description": "Stop a running workflow execution (NOT YET IMPLEMENTED)",
            "parameters": ["execution_id"],
        },
    ]
    return tools


# -----------------------------------------------------------------------------
# System State
# -----------------------------------------------------------------------------

@mcp.tool()
async def get_system_state() -> dict:
    """
    Get comprehensive system state including agents, executions, run states, and persistent state.

    Use this tool first to get a high-level view of the entire system before drilling
    into specific details. This is the starting point for understanding testbed health.

    Returns:
    - timestamp: current server time
    - agents: total, healthy (heartbeat <5min), unhealthy counts
    - executions: running count, completed in last hour
    - messages: count in last 10 minutes
    - run_states: current fast processing run states (phase, state, worker/slice counts)
    - persistent_state: system-wide persistent state (next IDs, etc.)
    - recent_events: last 10 system state events
    - health: 'healthy' if all agents OK, 'degraded' otherwise
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        now = timezone.now()
        recent_threshold = now - timedelta(minutes=5)

        # Agent stats - exclude EXITED agents from health calculation
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
            "health": "healthy" if active_agents == 0 or healthy_agents == active_agents else "degraded",
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

    Args:
        namespace: Filter to agents in this namespace (e.g., 'torre1', 'wenauseic')
        agent_type: Filter by type: 'daqsim', 'data', 'processing', 'fastmon', 'workflow_runner'
        status: Filter by status: 'OK', 'WARNING', 'ERROR', 'UNKNOWN', 'EXITED'
        execution_id: Filter to agents that participated in this workflow execution
        start_time: Filter to agents with heartbeat >= this ISO datetime
        end_time: Filter to agents with heartbeat <= this ISO datetime

    Returns list of agents with: name, agent_type, status, namespace, last_heartbeat,
    workflow_enabled, total_stf_processed
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        qs = SystemAgent.objects.all().order_by('-last_heartbeat')

        if namespace:
            qs = qs.filter(namespace=namespace)
        if agent_type:
            qs = qs.filter(agent_type=agent_type)
        if status:
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

        return [
            {
                "name": a.instance_name,
                "agent_type": a.agent_type,
                "status": a.status,
                "namespace": a.namespace,
                "last_heartbeat": a.last_heartbeat.isoformat() if a.last_heartbeat else None,
                "workflow_enabled": a.workflow_enabled,
                "total_stf_processed": a.total_stf_processed,
            }
            for a in qs
        ]

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
        return [
            {
                "name": n.name,
                "owner": n.owner,
                "description": n.description,
            }
            for n in Namespace.objects.all().order_by('name')
        ]

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

        return [
            {
                "workflow_name": w.workflow_name,
                "version": w.version,
                "workflow_type": w.workflow_type,
                "created_by": w.created_by,
                "created_at": w.created_at.isoformat() if w.created_at else None,
                "execution_count": w.execution_count,
            }
            for w in qs
        ]

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

        return [
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
        ]

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
    Useful for debugging workflow behavior or understanding what happened.

    Args:
        namespace: Filter to messages from this namespace
        execution_id: Filter to messages for this workflow execution
        agent: Filter to messages from this sender agent
        message_type: Filter by type (e.g., 'stf_created', 'processing_complete')
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

        return [
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
        ]

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

        results = []
        for r in qs[:100]:
            duration = None
            if r.start_time and r.end_time:
                duration = (r.end_time - r.start_time).total_seconds()

            results.append({
                "run_number": r.run_number,
                "start_time": r.start_time.isoformat() if r.start_time else None,
                "end_time": r.end_time.isoformat() if r.end_time else None,
                "duration_seconds": duration,
                "stf_file_count": r.stf_file_count,
            })

        return results

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

        return [
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
        ]

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

        return [
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
        ]

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
    level: str = None,
    search: str = None,
    start_time: str = None,
    end_time: str = None,
) -> list:
    """
    List application log entries with filtering.

    All agents log to the central database via Python's logging module.
    Use this tool to discover errors, debug issues, and understand system behavior.

    Args:
        app_name: Filter by application name (e.g., 'daq_simulator', 'data_agent')
        instance_name: Filter by agent instance name
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

        return [
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
            }
            for log in qs[:200]
        ]

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
            }
        except AppLog.DoesNotExist:
            return {"error": f"Log entry {log_id} not found."}

    return await fetch()


# -----------------------------------------------------------------------------
# Action Tools (Not Yet Implemented)
# -----------------------------------------------------------------------------

@mcp.tool()
async def start_workflow(workflow_name: str, namespace: str) -> dict:
    """
    Start a workflow execution. NOT YET IMPLEMENTED.

    This tool will eventually allow starting a workflow run from the LLM interface.
    Currently returns instructions for using the CLI instead.

    Args:
        workflow_name: Name of the workflow to run (use list_workflow_definitions to see available)
        namespace: Testbed namespace for this execution (use list_namespaces to see available)

    Returns:
        Status message with CLI instructions
    """
    return {
        "status": "not_implemented",
        "message": "Workflow start via MCP not yet implemented. Use CLI: swf-testbed run",
        "workflow_name": workflow_name,
        "namespace": namespace,
    }


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
