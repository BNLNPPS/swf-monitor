"""
PanDA REST API endpoints — JSON reads plus durable task-operation requests.

Under /swf-monitor/api/panda/. Reads are open; pause/resume requests and their
durable status are authenticated. The web tier only records and queues these
requests: the credentialed prod-ops agent invokes PanDA and records the verified
outcome.

Response shape matches the MCP tool responses for consistency: items / total_count
/ has_more / next_before_id / monitor_urls where applicable. Stability promise:
field names don't change silently; breaking changes would rename the endpoint.
"""
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status as http_status

from monitor_app.middleware import TunnelAuthentication

from . import operations, queries


_AUTH = [TunnelAuthentication, SessionAuthentication, TokenAuthentication]


def _int_param(request, name, default=None, min_value=None, max_value=None):
    """Parse an integer query param with bounds; return (value, error_response)."""
    raw = request.query_params.get(name)
    if raw is None:
        return default, None
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return None, Response(
            {'error': f'{name} must be an integer'},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    if min_value is not None and val < min_value:
        return None, Response(
            {'error': f'{name} must be >= {min_value}'},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    if max_value is not None and val > max_value:
        return None, Response(
            {'error': f'{name} must be <= {max_value}'},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    return val, None


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes([AllowAny])
def tasks_list(request):
    """GET /api/panda/tasks/ — list JEDI tasks with per-task job counts.

    Query params:
        days (int, default 7)           — modificationtime window
        status (str)                    — task status filter
        username (str, supports %)      — task owner
        taskname (str, supports %)      — task name
        reqid (int)
        workinggroup (str)              — e.g. EIC
        processingtype (str, supports %)
        limit (int, default 50, max 200)
        before_id (int)                 — cursor

    Returns:
        { items, total_count, has_more, next_before_id, summary, filters }
        Each task includes native JEDI fields (failurerate, progress, ...)
        plus nactive / nfinished / nfailed / nrunning / nretries aggregated
        from job tables, nfinalfailed / nfilesfinished from JEDI file-level
        accounting (retry-exhausted and finished input files), and computed
        helpers computed_failurerate (all failed job records) /
        computed_finalfailurerate (retry-exhausted input files only, used
        by alarms) / avg_retries_success (average retries per finished
        job; 0 = first-attempt pass).
    """
    limit, err = _int_param(request, 'limit', default=50, min_value=1, max_value=200)
    if err:
        return err
    days, err = _int_param(request, 'days', default=7, min_value=1)
    if err:
        return err
    reqid, err = _int_param(request, 'reqid')
    if err:
        return err
    before_id, err = _int_param(request, 'before_id')
    if err:
        return err

    result = queries.list_tasks(
        days=days,
        status=request.query_params.get('status'),
        username=request.query_params.get('username'),
        taskname=request.query_params.get('taskname'),
        reqid=reqid,
        workinggroup=request.query_params.get('workinggroup'),
        processingtype=request.query_params.get('processingtype'),
        limit=limit,
        before_id=before_id,
    )
    if 'error' in result:
        return Response(result, status=http_status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Reshape to match MCP tool response conventions.
    return Response({
        'items': result['tasks'],
        'total_count': result['total_in_window'],
        'has_more': result['pagination']['has_more'],
        'next_before_id': result['pagination']['next_before_id'],
        'summary': result['summary'],
        'filters': result['filters'],
    })


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes([AllowAny])
def jobs_list(request):
    """GET /api/panda/jobs/ — list PanDA jobs.

    Query params:
        days (int, default 7)   — modificationtime window
        status (str)            — jobstatus filter
        username (str, supports %)
        site (str, supports %)  — computingsite
        taskid (int)            — jeditaskid; scoping to a task returns
                                  its complete job population by default
        reqid (int)
        limit (int)             — explicit values always honored
        before_id (int)         — cursor

    Returns:
        { items, total_count, has_more, next_before_id, summary, filters }
        Each job carries creation, start, and end times, site, and
        output metadata — the per-worker record.
    """
    days, err = _int_param(request, 'days', default=7, min_value=1)
    if err:
        return err
    taskid, err = _int_param(request, 'taskid')
    if err:
        return err
    reqid, err = _int_param(request, 'reqid')
    if err:
        return err
    limit, err = _int_param(request, 'limit', min_value=1)
    if err:
        return err
    before_id, err = _int_param(request, 'before_id')
    if err:
        return err

    result = queries.list_jobs(
        days=days,
        status=request.query_params.get('status'),
        username=request.query_params.get('username'),
        site=request.query_params.get('site'),
        taskid=taskid,
        reqid=reqid,
        limit=limit,
        before_id=before_id,
    )
    if 'error' in result:
        return Response(result, status=http_status.HTTP_500_INTERNAL_SERVER_ERROR)

    return Response({
        'items': result['jobs'],
        'total_count': result['total_in_window'],
        'has_more': result['pagination']['has_more'],
        'next_before_id': result['pagination']['next_before_id'],
        'summary': result['summary'],
        'filters': result['filters'],
    })


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes([AllowAny])
def task_detail(request, jeditaskid):
    """GET /api/panda/tasks/<jeditaskid>/ — one task with per-task job counts."""
    task = queries.get_task(jeditaskid)
    if 'error' in task:
        if 'not found' in task['error']:
            return Response(task, status=http_status.HTTP_404_NOT_FOUND)
        return Response(task, status=http_status.HTTP_500_INTERNAL_SERVER_ERROR)
    return Response(task)


@api_view(['POST'])
@authentication_classes(_AUTH)
@permission_classes([IsAuthenticated])
def task_operation_request(request, jeditaskid):
    """Queue a durable manual pause/resume request for the prod-ops agent."""
    task = queries.get_task(jeditaskid)
    if 'error' in task:
        response_status = (http_status.HTTP_404_NOT_FOUND
                           if 'not found' in task['error']
                           else http_status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(task, status=response_status)
    try:
        record, created = operations.queue_task_operation(
            task=task,
            operation=str(request.data.get('operation') or ''),
            requested_by=getattr(request.user, 'username', '') or 'operator',
        )
    except operations.PandaTaskOperationError as exc:
        return Response({'error': exc.detail}, status=exc.status)
    payload = operations.serialize_operation(record)
    payload['created'] = created
    return Response(
        payload,
        status=(http_status.HTTP_202_ACCEPTED
                if created else http_status.HTTP_200_OK),
    )


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes([IsAuthenticated])
def task_operation_detail(request, operation_id):
    """Return the durable state used as the SSE reliability backstop."""
    from monitor_app.models import PandaTaskOperation

    try:
        record = PandaTaskOperation.objects.get(pk=operation_id)
    except PandaTaskOperation.DoesNotExist:
        return Response({'error': 'Operation not found.'},
                        status=http_status.HTTP_404_NOT_FOUND)
    return Response(operations.serialize_operation(record))


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def task_operation_update(request, operation_id):
    """Token-authenticated lifecycle callback from the prod-ops agent."""
    evidence = request.data.get('evidence') or {}
    if not isinstance(evidence, dict):
        return Response({'error': 'evidence must be an object'},
                        status=http_status.HTTP_400_BAD_REQUEST)
    try:
        record = operations.update_task_operation(
            operation_id,
            status=str(request.data.get('status') or ''),
            diagnostic=request.data.get('diagnostic') or '',
            observed_status=request.data.get('observed_status') or '',
            evidence=evidence,
        )
    except operations.PandaTaskOperationError as exc:
        return Response({'error': exc.detail}, status=exc.status)
    return Response(operations.serialize_operation(record))


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes([AllowAny])
def activity(request):
    """GET /api/panda/activity/ — aggregate counts by task and job status.

    Query params:
        days (int, default 1)
        username (str, supports %)
        site (str, supports %)
        workinggroup (str)
    """
    days, err = _int_param(request, 'days', default=1, min_value=1)
    if err:
        return err
    result = queries.get_activity(
        days=days,
        username=request.query_params.get('username'),
        site=request.query_params.get('site'),
        workinggroup=request.query_params.get('workinggroup'),
    )
    if 'error' in result:
        return Response(result, status=http_status.HTTP_500_INTERNAL_SERVER_ERROR)
    return Response(result)
