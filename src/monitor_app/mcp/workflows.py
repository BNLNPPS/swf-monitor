"""
Workflow and data pipeline MCP tools.

Includes: workflow definitions, executions, messages, runs, STF files, TF slices,
workflow control (start/stop), monitoring.
"""

import logging
from datetime import timedelta
from django.utils import timezone
from django.db.models import Count
from asgiref.sync import sync_to_async

from mcp_server import mcp_server as mcp

from ..models import Run, StfFile, TFSlice, AppLog, SystemAgent
from ..workflow_models import WorkflowDefinition, WorkflowExecution, WorkflowMessage
from .common import _parse_time, _default_start_time, _monitor_url, _get_testbed_config_path, _get_username

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Workflow Definitions
# -----------------------------------------------------------------------------

@mcp.tool()
async def swf_list_workflow_definitions(
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
    @sync_to_async
    def fetch():
        qs = WorkflowDefinition.objects.annotate(
            execution_count=Count('executions')
        ).order_by('workflow_name', '-version')

        if workflow_type:
            qs = qs.filter(workflow_type=workflow_type)
        if created_by:
            qs = qs.filter(created_by=created_by)

        MAX_ITEMS = 100
        total_count = qs.count()
        items = [
            {
                "workflow_name": w.workflow_name,
                "version": w.version,
                "workflow_type": w.workflow_type,
                "created_by": w.created_by,
                "created_at": w.created_at.isoformat() if w.created_at else None,
                "execution_count": w.execution_count,
            }
            for w in qs[:MAX_ITEMS]
        ]
        return {
            "items": items,
            "total_count": total_count,
            "has_more": total_count > MAX_ITEMS,
            "monitor_urls": [
                {"title": "Workflow Definitions", "url": _monitor_url("/workflow-definitions/")},
            ],
        }

    return await fetch()


# -----------------------------------------------------------------------------
# Workflow Executions
# -----------------------------------------------------------------------------

@mcp.tool()
async def swf_list_workflow_executions(
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

        if not currently_running:
            start = _parse_time(start_time) or _default_start_time(24)
            end = _parse_time(end_time)
            qs = qs.filter(start_time__gte=start)
            if end:
                qs = qs.filter(start_time__lte=end)

        params = []
        if namespace:
            params.append(f"namespace={namespace}")
        if status:
            params.append(f"status={status}")
        if executed_by:
            params.append(f"executed_by={executed_by}")
        query_string = "&".join(params)
        url = _monitor_url(f"/workflow-executions/?{query_string}" if query_string else "/workflow-executions/")

        MAX_ITEMS = 100
        total_count = qs.count()
        items = [
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
            for e in qs[:MAX_ITEMS]
        ]
        return {
            "items": items,
            "total_count": total_count,
            "has_more": total_count > MAX_ITEMS,
            "monitor_urls": [
                {"title": "Executions List", "url": url},
            ],
        }

    return await fetch()


@mcp.tool()
async def swf_get_workflow_execution(execution_id: str) -> dict:
    """
    Get detailed information about a specific workflow execution.

    Use swf_list_workflow_executions first to find execution IDs if needed.

    Args:
        execution_id: The execution ID (e.g., 'stf_datataking-wenauseic-0042')

    Returns: execution_id, workflow_name, namespace, status, executed_by,
    start_time, end_time, parameter_values, performance_metrics
    """
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
                    {"title": "Execution Detail", "url": _monitor_url(f"/workflow-executions/{e.execution_id}/")},
                ],
            }
        except WorkflowExecution.DoesNotExist:
            return {"error": f"Execution '{execution_id}' not found. Use swf_list_workflow_executions to see recent runs."}

    return await fetch()


# -----------------------------------------------------------------------------
# Messages
# -----------------------------------------------------------------------------

@mcp.tool()
async def swf_list_messages(
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

        start = _parse_time(start_time) or _default_start_time(1)
        end = _parse_time(end_time)
        qs = qs.filter(sent_at__gte=start)
        if end:
            qs = qs.filter(sent_at__lte=end)

        params = []
        if namespace:
            params.append(f"namespace={namespace}")
        if execution_id:
            params.append(f"execution_id={execution_id}")
        if message_type:
            params.append(f"message_type={message_type}")
        query_string = "&".join(params)
        url = _monitor_url(f"/workflow/messages/?{query_string}" if query_string else "/workflow/messages/")

        MAX_ITEMS = 200
        total_count = qs.count()
        items = [
            {
                "message_type": m.message_type,
                "sender_agent": m.sender_agent,
                "namespace": m.namespace,
                "sent_at": m.sent_at.isoformat() if m.sent_at else None,
                "execution_id": m.execution_id,
                "run_id": m.run_id,
                "payload_summary": str(m.message_content)[:200] if m.message_content else None,
            }
            for m in qs[:MAX_ITEMS]
        ]
        return {
            "items": items,
            "total_count": total_count,
            "has_more": total_count > MAX_ITEMS,
            "monitor_urls": [
                {"title": "Messages List", "url": url},
            ],
        }

    return await fetch()


# -----------------------------------------------------------------------------
# Runs
# -----------------------------------------------------------------------------

@mcp.tool()
async def swf_list_runs(
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
    @sync_to_async
    def fetch():
        qs = Run.objects.annotate(
            stf_file_count=Count('stf_files')
        ).order_by('-start_time')

        start = _parse_time(start_time) or _default_start_time(168)
        end = _parse_time(end_time)
        qs = qs.filter(start_time__gte=start)
        if end:
            qs = qs.filter(start_time__lte=end)

        MAX_ITEMS = 100
        total_count = qs.count()
        items = []
        for r in qs[:MAX_ITEMS]:
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
            "total_count": total_count,
            "has_more": total_count > MAX_ITEMS,
            "monitor_urls": [
                {"title": "Runs List", "url": _monitor_url("/runs/")},
            ],
        }

    return await fetch()


@mcp.tool()
async def swf_get_run(run_number: int) -> dict:
    """
    Get detailed information about a specific run.

    Args:
        run_number: The run number (required)

    Returns: run_number, start_time, end_time, duration_seconds, run_conditions,
    file_stats (counts by status)
    """
    @sync_to_async
    def fetch():
        try:
            r = Run.objects.get(run_number=run_number)

            duration = None
            if r.start_time and r.end_time:
                duration = (r.end_time - r.start_time).total_seconds()

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
            return {"error": f"Run {run_number} not found. Use swf_list_runs to see available runs."}

    return await fetch()


# -----------------------------------------------------------------------------
# STF Files
# -----------------------------------------------------------------------------

@mcp.tool()
async def swf_list_stf_files(
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

        start = _parse_time(start_time) or (None if run_number else _default_start_time(24))
        end = _parse_time(end_time)
        if start:
            qs = qs.filter(created_at__gte=start)
        if end:
            qs = qs.filter(created_at__lte=end)

        params = []
        if run_number:
            params.append(f"run_number={run_number}")
        if status:
            params.append(f"status={status}")
        query_string = "&".join(params)
        url = _monitor_url(f"/stf-files/?{query_string}" if query_string else "/stf-files/")

        MAX_ITEMS = 100
        total_count = qs.count()
        items = [
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
            for f in qs[:MAX_ITEMS]
        ]
        return {
            "items": items,
            "total_count": total_count,
            "has_more": total_count > MAX_ITEMS,
            "monitor_urls": [
                {"title": "STF Files List", "url": url},
            ],
        }

    return await fetch()


@mcp.tool()
async def swf_get_stf_file(file_id: str = None, stf_filename: str = None) -> dict:
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
            return {"error": "STF file not found. Use swf_list_stf_files to see available files."}

    return await fetch()


# -----------------------------------------------------------------------------
# TF Slices (Fast Processing)
# -----------------------------------------------------------------------------

@mcp.tool()
async def swf_list_tf_slices(
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

        has_context = run_number or stf_filename or tf_filename
        start = _parse_time(start_time) or (None if has_context else _default_start_time(24))
        end = _parse_time(end_time)
        if start:
            qs = qs.filter(created_at__gte=start)
        if end:
            qs = qs.filter(created_at__lte=end)

        params = []
        if run_number:
            params.append(f"run_number={run_number}")
        if status:
            params.append(f"status={status}")
        query_string = "&".join(params)
        url = _monitor_url(f"/tf-slices/?{query_string}" if query_string else "/tf-slices/")

        MAX_ITEMS = 200
        total_count = qs.count()
        items = [
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
            for s in qs[:MAX_ITEMS]
        ]
        return {
            "items": items,
            "total_count": total_count,
            "has_more": total_count > MAX_ITEMS,
            "monitor_urls": [
                {"title": "TF Slices List", "url": url},
            ],
        }

    return await fetch()


@mcp.tool()
async def swf_get_tf_slice(tf_filename: str, slice_id: int) -> dict:
    """
    Get detailed information about a specific TF slice.

    Args:
        tf_filename: The TF filename (required)
        slice_id: The slice ID within the TF (required, typically 1-15)

    Returns: slice_id, tf_filename, stf_filename, run_number, tf_first, tf_last,
    tf_count, status, retries, assigned_worker, assigned_at, completed_at, metadata
    """
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
            return {"error": f"TF slice {slice_id} for {tf_filename} not found. Use swf_list_tf_slices to see available slices."}

    return await fetch()


# -----------------------------------------------------------------------------
# Workflow Control
# -----------------------------------------------------------------------------

@mcp.tool()
async def swf_start_workflow(
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

    @sync_to_async
    def do_start():
        import os
        from pathlib import Path
        from ..activemq_connection import ActiveMQConnectionManager

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
                toml_params = toml_data.get('parameters', {})
                if config_source == 'SWF_TESTBED_CONFIG':
                    logger.info(f"Using config from SWF_TESTBED_CONFIG: {testbed_toml.name}")
            except Exception as e:
                logger.warning(f"Failed to read {testbed_toml}: {e}")

        actual_workflow_name = workflow_name or toml_workflow_name or 'stf_datataking'
        actual_namespace = namespace or toml_namespace
        actual_config = config or toml_config or 'fast_processing_default'

        # If namespace not from caller or TOML, use the running agent manager's
        # namespace from DB — it reflects the actual loaded config
        if not actual_namespace:
            try:
                cutoff = timezone.now() - timedelta(minutes=5)
                am = SystemAgent.objects.filter(
                    agent_type='agent_manager',
                    last_heartbeat__gte=cutoff
                ).order_by('-last_heartbeat').first()
                if am and am.namespace:
                    actual_namespace = am.namespace
                    logger.info(f"Using namespace '{am.namespace}' from agent manager '{am.instance_name}'")
            except Exception:
                pass
        if not actual_namespace:
            actual_namespace = 'torre1'
        actual_realtime = realtime if realtime is not None else (toml_realtime if toml_realtime is not None else True)

        params = dict(toml_params)
        if stf_count is not None:
            params['stf_count'] = stf_count
        if physics_period_count is not None:
            params['physics_period_count'] = physics_period_count
        if physics_period_duration is not None:
            params['physics_period_duration'] = physics_period_duration
        if stf_interval is not None:
            params['stf_interval'] = stf_interval

        params['namespace'] = actual_namespace

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
                "note": "Workflow runs asynchronously. Use swf_list_workflow_executions to monitor."
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
async def swf_stop_workflow(execution_id: str) -> dict:
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

    @sync_to_async
    def do_stop():
        from ..activemq_connection import ActiveMQConnectionManager

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
async def swf_end_execution(execution_id: str) -> dict:
    """
    End a running workflow execution by setting its status to 'terminated'.

    Use this to clean up stale or stuck executions that are still marked as 'running'.
    This is a state change only - no data is deleted. The action is logged.

    Args:
        execution_id: The execution ID to end (use list_workflow_executions to find running ones)

    Returns:
        Success/failure status with details
    """
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


# -----------------------------------------------------------------------------
# Workflow Monitoring
# -----------------------------------------------------------------------------

@mcp.tool()
async def swf_get_workflow_monitor(execution_id: str) -> dict:
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
    import logging as py_logging

    @sync_to_async
    def fetch():
        try:
            execution = WorkflowExecution.objects.get(execution_id=execution_id)
            db_status = execution.status
            db_start_time = execution.start_time
            db_end_time = execution.end_time
        except WorkflowExecution.DoesNotExist:
            return {"error": f"Execution '{execution_id}' not found"}

        duration_seconds = None
        if db_start_time and db_end_time:
            duration_seconds = (db_end_time - db_start_time).total_seconds()

        messages = WorkflowMessage.objects.filter(
            execution_id=execution_id
        ).order_by('sent_at')

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
                {"title": "Execution Detail", "url": _monitor_url(f"/workflow-executions/{execution_id}/")},
            ],
        }

    return await fetch()


@mcp.tool()
async def swf_list_workflow_monitors() -> list:
    """
    List recent workflow executions that can be monitored.

    Returns executions from the last 24 hours with their current status,
    allowing you to pick one to monitor with get_workflow_monitor().

    Returns list of executions with: execution_id, status, start_time, stf_count
    """
    @sync_to_async
    def fetch():
        now = timezone.now()
        qs = WorkflowExecution.objects.filter(
            start_time__gte=now - timedelta(hours=24)
        ).order_by('-start_time')

        MAX_ITEMS = 20
        total_count = qs.count()
        items = []
        for e in qs[:MAX_ITEMS]:
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
            "total_count": total_count,
            "has_more": total_count > MAX_ITEMS,
            "monitor_urls": [
                {"title": "Executions List", "url": _monitor_url("/workflow-executions/")},
            ],
        }

    return await fetch()


@mcp.tool()
async def swf_send_message(message: str, message_type: str = "announcement", metadata: dict = None) -> dict:
    """
    Send a message to the workflow monitoring stream.

    Use for testing the message pipeline, announcements to colleagues,
    or any other broadcast purpose.

    The sender is automatically identified as '{username}-personal-agent'.

    Args:
        message: The message text to send
        message_type: Type of message - 'announcement', 'status', 'test', etc.
                      If 'test', namespace is omitted. Otherwise uses configured namespace.
        metadata: Optional additional key-value data to include

    Returns:
        Success/failure status with message details
    """
    import json
    from datetime import datetime

    @sync_to_async
    def do_send():
        from ..activemq_connection import ActiveMQConnectionManager

        username = _get_username()
        sender = f"{username}-personal-agent"

        namespace = None
        if message_type != 'test':
            testbed_toml, _ = _get_testbed_config_path()
            if testbed_toml and testbed_toml.exists():
                try:
                    import tomllib
                    with open(testbed_toml, 'rb') as f:
                        toml_data = tomllib.load(f)
                    namespace = toml_data.get('testbed', {}).get('namespace')
                except Exception:
                    pass

        msg = {
            'msg_type': message_type,
            'sender': sender,
            'namespace': namespace,
            'message': message,
            'timestamp': datetime.now().isoformat(),
            'source': 'mcp_send_message',
        }
        if metadata:
            msg['metadata'] = metadata

        topic = '/topic/epictopic'
        mq = ActiveMQConnectionManager()
        if mq.send_message(topic, json.dumps(msg)):
            logger.info(f"MCP send_message: sent {message_type} from {sender}")
            return {
                "success": True,
                "message": "Message sent to monitoring stream",
                "sender": sender,
                "message_type": message_type,
                "namespace": namespace,
                "content": message,
            }
        else:
            return {
                "success": False,
                "error": "Failed to send message to ActiveMQ. Is the message broker running?",
            }

    return await do_send()
