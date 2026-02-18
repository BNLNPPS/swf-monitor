"""
System and infrastructure MCP tools.

Includes: system state, agents, namespaces, logs, testbed management.
"""

import logging
from datetime import timedelta
from django.utils import timezone
from asgiref.sync import sync_to_async

from mcp_server import mcp_server as mcp

from ..models import SystemAgent, RunState, PersistentState, SystemStateEvent, AppLog
from ..workflow_models import WorkflowExecution, WorkflowMessage, Namespace
from .common import _parse_time, _default_start_time, _monitor_url, _get_testbed_config_path, _get_username

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# System State
# -----------------------------------------------------------------------------

@mcp.tool()
async def swf_get_system_state(username: str = None) -> dict:
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

    username = _get_username(username)
    swf_home = os.getenv('SWF_HOME', f'/data/{username}/github')

    @sync_to_async
    def fetch():
        now = timezone.now()
        recent_threshold = now - timedelta(minutes=5)

        # User context from testbed config
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

        # Agent manager status
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

        # Workflow runner status
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
            any_runner = SystemAgent.objects.filter(
                agent_type__in=['DAQ_Simulator', 'workflow_runner'],
                namespace=user_namespace,
            ).exclude(operational_state='EXITED').first()
            if any_runner:
                workflow_runner["status"] = "unhealthy"
                workflow_runner["name"] = any_runner.instance_name
                workflow_runner["last_heartbeat"] = any_runner.last_heartbeat.isoformat() if any_runner.last_heartbeat else None

        ready_to_run = workflow_runner["status"] == "healthy"

        # Last execution for user's namespace
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

        # Errors in last hour
        errors_last_hour = 0
        if user_namespace:
            import logging as py_logging
            errors_last_hour = AppLog.objects.filter(
                level__gte=py_logging.ERROR,
                timestamp__gte=now - timedelta(hours=1),
                extra_data__namespace=user_namespace,
            ).count()

        # Global agent stats
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

        # Run states
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
async def swf_list_agents(
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
    @sync_to_async
    def fetch():
        qs = SystemAgent.objects.all().order_by('-last_heartbeat')

        if namespace:
            qs = qs.filter(namespace=namespace)
        if agent_type:
            qs = qs.filter(agent_type=agent_type)

        if status is None:
            qs = qs.exclude(status='EXITED')
        elif status.lower() != 'all':
            qs = qs.filter(status__iexact=status)

        start = _parse_time(start_time)
        end = _parse_time(end_time)
        if start:
            qs = qs.filter(last_heartbeat__gte=start)
        if end:
            qs = qs.filter(last_heartbeat__lte=end)

        if execution_id:
            agent_names = WorkflowMessage.objects.filter(
                execution_id=execution_id
            ).values_list('sender_agent', flat=True).distinct()
            qs = qs.filter(instance_name__in=agent_names)

        params = []
        if namespace:
            params.append(f"namespace={namespace}")
        if agent_type:
            params.append(f"agent_type={agent_type}")
        if status and status.lower() != 'all':
            params.append(f"status={status}")
        query_string = "&".join(params)
        url = _monitor_url(f"/workflow/agents/?{query_string}" if query_string else "/workflow/agents/")

        MAX_ITEMS = 100
        total_count = qs.count()
        items = [
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
            for a in qs[:MAX_ITEMS]
        ]
        return {
            "items": items,
            "total_count": total_count,
            "has_more": total_count > MAX_ITEMS,
            "monitor_urls": [
                {"title": "Agents List", "url": url},
            ],
        }

    return await fetch()


@mcp.tool()
async def swf_get_agent(name: str) -> dict:
    """
    Get detailed information about a specific agent.

    Use swf_list_agents first to see available agent names if you don't know them.

    Args:
        name: The exact agent instance name (e.g., 'daq_simulator_torre1')

    Returns: name, agent_type, status, namespace, last_heartbeat, description,
    workflow_enabled, current_stf_count, total_stf_processed, metadata
    """
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
                    {"title": "Agent Detail", "url": _monitor_url(f"/workflow/agents/{a.instance_name}/")},
                ],
            }
        except SystemAgent.DoesNotExist:
            return {"error": f"Agent '{name}' not found. Use swf_list_agents to see available agents."}

    return await fetch()


# -----------------------------------------------------------------------------
# Namespaces
# -----------------------------------------------------------------------------

@mcp.tool()
async def swf_list_namespaces() -> list:
    """
    List all testbed namespaces.

    Namespaces provide isolation between different users' workflow runs.
    Each namespace has its own set of agents and workflow executions.

    Returns list of namespaces with: name, owner, description
    """
    @sync_to_async
    def fetch():
        qs = Namespace.objects.all().order_by('name')
        MAX_ITEMS = 100
        total_count = qs.count()
        items = [
            {
                "name": n.name,
                "owner": n.owner,
                "description": n.description,
            }
            for n in qs[:MAX_ITEMS]
        ]
        return {
            "items": items,
            "total_count": total_count,
            "has_more": total_count > MAX_ITEMS,
            "monitor_urls": [
                {"title": "Namespaces List", "url": _monitor_url("/namespaces/")},
            ],
        }

    return await fetch()


@mcp.tool()
async def swf_get_namespace(
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
    @sync_to_async
    def fetch():
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

        start = _parse_time(start_time) or _default_start_time(24)
        end = _parse_time(end_time) or timezone.now()

        agent_count = SystemAgent.objects.filter(namespace=namespace).count()

        execution_qs = WorkflowExecution.objects.filter(
            namespace=namespace,
            start_time__gte=start,
            start_time__lte=end,
        )
        execution_count = execution_qs.count()
        active_users = list(execution_qs.values_list('executed_by', flat=True).distinct())

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
                {"title": "Namespace Detail", "url": _monitor_url(f"/workflow/namespaces/{namespace}/")},
            ],
        }

    return await fetch()


# -----------------------------------------------------------------------------
# Logs
# -----------------------------------------------------------------------------

@mcp.tool()
async def swf_list_logs(
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

    @sync_to_async
    def fetch():
        qs = AppLog.objects.all().order_by('-timestamp')

        if app_name:
            qs = qs.filter(app_name=app_name)
        if instance_name:
            qs = qs.filter(instance_name=instance_name)
        if execution_id:
            qs = qs.filter(extra_data__execution_id=execution_id)

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

        if search:
            qs = qs.filter(message__icontains=search)

        start = _parse_time(start_time) or _default_start_time(24)
        end = _parse_time(end_time)
        qs = qs.filter(timestamp__gte=start)
        if end:
            qs = qs.filter(timestamp__lte=end)

        params = []
        if instance_name:
            params.append(f"instance_name={instance_name}")
        if execution_id:
            params.append(f"execution_id={execution_id}")
        if level:
            params.append(f"level={level}")
        query_string = "&".join(params)
        url = _monitor_url(f"/logs/?{query_string}" if query_string else "/logs/")

        MAX_ITEMS = 200
        total_count = qs.count()
        items = [
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
            for log in qs[:MAX_ITEMS]
        ]
        return {
            "items": items,
            "total_count": total_count,
            "has_more": total_count > MAX_ITEMS,
            "monitor_urls": [
                {"title": "Logs List", "url": url},
            ],
        }

    return await fetch()


@mcp.tool()
async def swf_get_log_entry(log_id: int) -> dict:
    """
    Get full details of a specific log entry.

    Args:
        log_id: The log entry ID (from list_logs)

    Returns: Full log entry with all fields including extra_data
    """
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
# Agent Management
# -----------------------------------------------------------------------------

@mcp.tool()
async def swf_kill_agent(name: str) -> dict:
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

    current_host = socket.gethostname()

    @sync_to_async
    def do_kill():
        try:
            agent = SystemAgent.objects.get(instance_name=name)
        except SystemAgent.DoesNotExist:
            return {
                "success": False,
                "error": f"Agent '{name}' not found. Use swf_list_agents to see available agents.",
            }

        pid = agent.pid
        hostname = agent.hostname
        old_state = agent.operational_state
        killed = False
        kill_error = None

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
# User Testbed Management
# -----------------------------------------------------------------------------

@mcp.tool()
async def swf_check_agent_manager(username: str = None) -> dict:
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
    username = _get_username(username)

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
                "status": agent.status,
                "description": agent.description,
                "control_queue": control_queue,
                "agents_running": metadata.get('agents_running', False),
                "supervisord_healthy": metadata.get('supervisord_healthy'),
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
async def swf_start_user_testbed(username: str = None, config_name: str = None) -> dict:
    """
    Start a user's testbed via their agent manager daemon.

    Sends a start_testbed command to the user's agent manager, which then
    starts supervisord-managed agents. The agent manager must be running first.

    Refuses to start if workflow agents are already running. User must call
    stop_user_testbed first to ensure clean slate.

    Args:
        username: The username whose testbed to start. If not provided, uses current user.
        config_name: Config file name in workflows/ directory (default: testbed.toml).
                     If not provided, agent manager uses its already-loaded config
                     (from SWF_TESTBED_CONFIG env var or default).

    Returns:
        Success/failure status. If agent manager is not running, provides instructions.
    """
    import json
    from datetime import datetime

    username = _get_username(username)

    # First check if agent manager is alive
    manager_status = await swf_check_agent_manager(username)
    if not manager_status.get('alive'):
        return {
            "success": False,
            "error": f"Agent manager for '{username}' is not running",
            "how_to_start": f"Run 'testbed agent-manager' in {username}'s swf-testbed directory",
            "username": username,
        }

    # Check if workflow agents are already running
    testbed_status = await swf_get_testbed_status(username)
    running_agents = [a['name'] for a in testbed_status.get('agents', []) if a.get('status') == 'running']
    if running_agents:
        return {
            "success": False,
            "error": f"Cannot start: workflow agents already running: {running_agents}",
            "how_to_fix": "Call stop_user_testbed first to stop existing agents",
            "username": username,
            "running_agents": running_agents,
        }

    control_queue = f'/queue/agent_control.{username}'

    @sync_to_async
    def send_command():
        from ..activemq_connection import ActiveMQConnectionManager
        mq = ActiveMQConnectionManager()

        start_msg = {
            'command': 'start_testbed',
            'timestamp': datetime.now().isoformat(),
            'source': 'mcp'
        }
        if config_name:
            start_msg['config_name'] = config_name

        config_desc = config_name or 'agent manager default'

        if mq.send_message(control_queue, json.dumps(start_msg)):
            logger.info(
                f"MCP start_user_testbed: sent start_testbed for '{username}' "
                f"(config={config_desc})"
            )
            return {
                "success": True,
                "message": "Start command sent to agent manager",
                "username": username,
                "config": config_desc,
                "control_queue": control_queue,
                "note": "Agents will start asynchronously. Use swf_get_testbed_status to verify.",
            }
        else:
            return {
                "success": False,
                "error": "Failed to send start_testbed command to ActiveMQ",
                "username": username,
            }

    return await send_command()


@mcp.tool()
async def swf_stop_user_testbed(username: str = None) -> dict:
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
    from datetime import datetime

    username = _get_username(username)

    manager_status = await swf_check_agent_manager(username)
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
        from ..activemq_connection import ActiveMQConnectionManager

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
                "note": "Agents will stop asynchronously. Use swf_list_agents to verify.",
            }
        else:
            return {
                "success": False,
                "error": "Failed to send message to ActiveMQ. Is the message broker running?",
                "username": username,
            }

    return await send_command()


@mcp.tool()
async def swf_get_testbed_status(username: str = None) -> dict:
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
    username = _get_username(username)

    manager_status = await swf_check_agent_manager(username)
    namespace = manager_status.get('namespace')

    @sync_to_async
    def fetch_agents():
        now = timezone.now()
        healthy_threshold = now - timedelta(minutes=2)

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

    alive = manager_status.get('alive', False)
    sv_healthy = manager_status.get('supervisord_healthy')
    manager_error = manager_status.get('status') == 'ERROR'
    ready = alive and not manager_error and agents_data['running'] == 0

    result = {
        'username': username,
        'agent_manager': {
            'alive': alive,
            'namespace': namespace,
            'operational_state': manager_status.get('operational_state'),
            'status': manager_status.get('status'),
            'last_heartbeat': manager_status.get('last_heartbeat'),
            'supervisord_healthy': sv_healthy,
        },
        'agents': agents_data['agents'],
        'summary': {
            'running': agents_data['running'],
            'stopped': agents_data['stopped'],
        },
        'ready': ready,
    }

    if not alive:
        result['error'] = 'Agent manager is not running. Run /check-testbed to bootstrap infrastructure.'
    elif manager_error:
        result['error'] = f"Agent manager reports ERROR: {manager_status.get('description', 'unknown')}"
    elif agents_data['running'] == 0:
        result['note'] = 'Testbed is idle and ready to start'

    return result
