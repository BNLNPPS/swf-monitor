"""
MCP Tools for ePIC Streaming Workflow Testbed Monitor.

These tools enable LLM-based natural language interaction with the testbed,
allowing users to query system status, agent health, workflow executions,
and messages.
"""

from datetime import timedelta
from django.utils import timezone
from mcp_server import mcp_server as mcp

from .models import SystemAgent
from .workflow_models import (
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowMessage,
    Namespace,
)


# -----------------------------------------------------------------------------
# System Status
# -----------------------------------------------------------------------------

@mcp.tool()
async def get_system_status() -> dict:
    """
    Get overall testbed health status including agent counts and recent activity.

    Use this tool first to get a high-level view of the system before drilling
    into specific agents, workflows, or messages.

    Returns a summary with:
    - agents: total count, healthy count (heartbeat within 5 min), unhealthy count
    - executions: currently running count, completed in last hour
    - messages: count received in last 10 minutes
    - status: 'healthy' if all agents OK, 'degraded' otherwise
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        now = timezone.now()
        recent_threshold = now - timedelta(minutes=5)

        total_agents = SystemAgent.objects.count()
        healthy_agents = SystemAgent.objects.filter(
            last_heartbeat__gte=recent_threshold,
            status='OK'
        ).count()

        running_executions = WorkflowExecution.objects.filter(status='running').count()
        recent_completed = WorkflowExecution.objects.filter(
            status='completed',
            end_time__gte=now - timedelta(hours=1)
        ).count()

        recent_messages = WorkflowMessage.objects.filter(
            sent_at__gte=now - timedelta(minutes=10)
        ).count()

        return {
            "timestamp": now.isoformat(),
            "agents": {
                "total": total_agents,
                "healthy": healthy_agents,
                "unhealthy": total_agents - healthy_agents,
            },
            "executions": {
                "running": running_executions,
                "completed_last_hour": recent_completed,
            },
            "messages_last_10min": recent_messages,
            "status": "healthy" if healthy_agents == total_agents else "degraded",
        }

    return await fetch()


# -----------------------------------------------------------------------------
# Agents
# -----------------------------------------------------------------------------

@mcp.tool()
async def list_agents(namespace: str = None) -> list:
    """
    List all registered agents in the testbed with their current status.

    Agents are processes that participate in workflows (e.g., DAQ simulator,
    data agent, processing agent, fast monitoring agent). Each agent sends
    periodic heartbeats to indicate it is alive.

    Args:
        namespace: Optional. Filter to only show agents in this namespace.
                   Namespaces isolate different users' workflow runs.

    Returns list of agents, each with:
    - name: unique agent identifier
    - agent_type: what kind of agent (e.g., 'daq_simulator', 'processing')
    - status: 'OK' or error state
    - namespace: which namespace this agent belongs to
    - last_heartbeat: when the agent last reported in (ISO timestamp)
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        qs = SystemAgent.objects.all().order_by('-last_heartbeat')
        if namespace:
            qs = qs.filter(namespace=namespace)
        return [
            {
                "name": a.name,
                "agent_type": a.agent_type,
                "status": a.status,
                "namespace": a.namespace,
                "last_heartbeat": a.last_heartbeat.isoformat() if a.last_heartbeat else None,
            }
            for a in qs
        ]

    return await fetch()


@mcp.tool()
async def get_agent(name: str) -> dict:
    """
    Get detailed information about a specific agent by name.

    Use list_agents first to see available agent names if you don't know them.

    Args:
        name: The exact agent name (e.g., 'daq_simulator_torre1', 'processing_agent_torre1')

    Returns:
    - name: agent identifier
    - agent_type: role in the workflow
    - status: 'OK' or error state
    - namespace: isolation namespace
    - last_heartbeat: when agent last reported (ISO timestamp)
    - metadata: additional agent-specific data
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        try:
            agent = SystemAgent.objects.get(name=name)
            return {
                "name": agent.name,
                "agent_type": agent.agent_type,
                "status": agent.status,
                "last_heartbeat": agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
                "namespace": agent.namespace,
                "metadata": agent.metadata,
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

    Returns list of namespaces, each with:
    - name: namespace identifier (e.g., 'torre1', 'wenauseic')
    - owner: who created/owns this namespace
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        return [
            {"name": n.name, "owner": n.owner}
            for n in Namespace.objects.all().order_by('name')
        ]

    return await fetch()


# -----------------------------------------------------------------------------
# Workflow Definitions and Executions
# -----------------------------------------------------------------------------

@mcp.tool()
async def list_workflow_definitions() -> list:
    """
    List available workflow definitions that can be executed.

    Workflow definitions describe the structure of a workflow (what stages,
    what agents needed). Common workflows include 'stf_datataking' for
    streaming data acquisition simulation.

    Returns list of definitions, each with:
    - workflow_name: identifier used to start the workflow
    - version: workflow version
    - workflow_type: category of workflow
    - description: what the workflow does
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        return [
            {
                "workflow_name": w.workflow_name,
                "version": w.version,
                "workflow_type": w.workflow_type,
                "description": w.description,
            }
            for w in WorkflowDefinition.objects.all().order_by('workflow_name')
        ]

    return await fetch()


@mcp.tool()
async def list_workflow_executions(namespace: str = None, status: str = None, hours: int = 24) -> list:
    """
    List recent workflow executions.

    Shows workflow runs from the past N hours. Use this to see what workflows
    have been running, their status, and timing.

    Args:
        namespace: Optional. Filter to executions in this namespace only.
        status: Optional. Filter by status: 'running', 'completed', 'failed', 'cancelled'
        hours: How far back to look. Default 24 hours.

    Returns list of executions, each with:
    - execution_id: unique identifier for this run
    - workflow_name: which workflow definition was executed
    - status: current state
    - namespace: isolation namespace
    - start_time, end_time: timing (ISO timestamps)
    - executed_by: who started this execution
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        since = timezone.now() - timedelta(hours=hours)
        qs = WorkflowExecution.objects.select_related('workflow_definition').filter(
            start_time__gte=since
        ).order_by('-start_time')
        if namespace:
            qs = qs.filter(namespace=namespace)
        if status:
            qs = qs.filter(status=status)
        return [
            {
                "execution_id": e.execution_id,
                "workflow_name": e.workflow_definition.workflow_name if e.workflow_definition else None,
                "status": e.status,
                "namespace": e.namespace,
                "start_time": e.start_time.isoformat() if e.start_time else None,
                "end_time": e.end_time.isoformat() if e.end_time else None,
                "executed_by": e.executed_by,
            }
            for e in qs
        ]

    return await fetch()


@mcp.tool()
async def get_workflow_execution(execution_id: str) -> dict:
    """
    Get detailed information about a specific workflow execution.

    Use list_workflow_executions first to find execution IDs if needed.

    Args:
        execution_id: The execution ID (e.g., 'stf_datataking-wenauseic-0042')

    Returns:
    - execution_id: unique identifier
    - workflow_name: which workflow was executed
    - status: 'running', 'completed', 'failed', or 'cancelled'
    - namespace: isolation namespace
    - start_time, end_time: timing (ISO timestamps)
    - executed_by: who started this execution
    - parameter_values: configuration parameters used for this run
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
                "status": e.status,
                "namespace": e.namespace,
                "start_time": e.start_time.isoformat() if e.start_time else None,
                "end_time": e.end_time.isoformat() if e.end_time else None,
                "executed_by": e.executed_by,
                "parameter_values": e.parameter_values,
            }
        except WorkflowExecution.DoesNotExist:
            return {"error": f"Execution '{execution_id}' not found. Use list_workflow_executions to see recent runs."}

    return await fetch()


# -----------------------------------------------------------------------------
# Messages
# -----------------------------------------------------------------------------

@mcp.tool()
async def list_messages(namespace: str = None, sender: str = None, message_type: str = None, minutes: int = 30) -> list:
    """
    List recent workflow messages.

    Messages are sent between agents during workflow execution. They record
    events like STF creation, processing completion, state transitions, etc.
    Useful for debugging workflow behavior or understanding what happened.

    Args:
        namespace: Optional. Filter to messages from this namespace.
        sender: Optional. Filter to messages from this sender agent.
        message_type: Optional. Filter by message type (e.g., 'stf_created', 'processing_complete')
        minutes: How far back to look. Default 30 minutes.

    Returns list of messages (max 100), each with:
    - message_type: what kind of event
    - sender_agent: which agent sent it
    - namespace: isolation namespace
    - sent_at: when sent (ISO timestamp)
    - payload_summary: first 200 chars of message content
    """
    from asgiref.sync import sync_to_async

    @sync_to_async
    def fetch():
        since = timezone.now() - timedelta(minutes=minutes)
        qs = WorkflowMessage.objects.filter(sent_at__gte=since).order_by('-sent_at')
        if namespace:
            qs = qs.filter(namespace=namespace)
        if sender:
            qs = qs.filter(sender_agent=sender)
        if message_type:
            qs = qs.filter(message_type=message_type)
        return [
            {
                "message_type": m.message_type,
                "sender_agent": m.sender_agent,
                "namespace": m.namespace,
                "sent_at": m.sent_at.isoformat() if m.sent_at else None,
                "payload_summary": str(m.payload)[:200] if m.payload else None,
            }
            for m in qs[:100]
        ]

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
        "message": f"Workflow start via MCP not yet implemented. Use CLI: swf-testbed run",
        "workflow_name": workflow_name,
        "namespace": namespace,
    }


@mcp.tool()
async def stop_workflow(execution_id: str) -> dict:
    """
    Stop a running workflow execution. NOT YET IMPLEMENTED.

    This tool will eventually allow stopping a running workflow from the LLM interface.
    Currently returns instructions for manual intervention.

    Args:
        execution_id: The execution ID to stop (use list_workflow_executions to find running ones)

    Returns:
        Status message
    """
    return {
        "status": "not_implemented",
        "message": "Workflow stop via MCP not yet implemented",
        "execution_id": execution_id,
    }
