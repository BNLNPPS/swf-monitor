"""Capcom endpoints: display-ready SWF state and buffered SWF events.

Capcom polls the open read endpoints every few minutes (through the
swf-remote proxy for external reach). Each entry under 'states' is shaped
exactly as tjai's capcom.set_state(source, value, color, url) expects,
following the pax-eden Ahbazon producer, so the capcom-side collector can
apply each entry as delivered.

Discrete SWF events (the campaign-delivery and task-operation feeds) are
buffered here in CapcomNotice rows and served by the open notices
endpoint; the consumer drains them with a since-cursor on its own poll.
SWF never posts into an external feed and holds no external credential —
the producing agent writes notices to this monitor with its ordinary
monitor token, and the feed system's credentials stay entirely on the
feed system's side.
"""

import logging
import re
from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone
from urllib.parse import urlencode

from django.http import JsonResponse
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.authentication import (SessionAuthentication,
                                           TokenAuthentication)
from rest_framework.decorators import (api_view, authentication_classes,
                                       permission_classes)
from rest_framework.permissions import (IsAuthenticated,
                                        IsAuthenticatedOrReadOnly)
from rest_framework.response import Response

from ..models import NoticeSubscription
from ..serializers import NoticeSubscriptionSerializer

logger = logging.getLogger(__name__)

# The notice buffer is a hand-off, not an archive: consumers keep their
# own history, so rows past this window are pruned on each ingest.
NOTICE_RETENTION_DAYS = 30
# Page cap on one notices read; 'more' flags a truncated response.
NOTICE_PAGE_MAX = 500

REMOTE_FACE = 'https://epic-devcloud.org/prod'
CAPCOM_WORKLOAD_CHECKS = frozenset({'stale-state'})
USERNAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')
CAPCOM_VALUE_MAX_CHARS = 50


def _compact_value(value):
    """Keep state-tile summaries within Capcom's usual 50 characters."""
    value = str(value)
    if len(value) <= CAPCOM_VALUE_MAX_CHARS:
        return value
    return value[:CAPCOM_VALUE_MAX_CHARS - 1].rstrip() + '…'


def _running_panda_task_count():
    """Return the count of PanDA tasks currently in status running."""
    from django.db import connections
    from ..panda.queries import PANDA_SCHEMA

    connection = connections['panda']
    with connection.cursor() as cursor:
        cursor.execute(
            f'SELECT COUNT(*) FROM "{PANDA_SCHEMA}"."jedi_tasks" '
            'WHERE "status" = %s',
            ['running'],
        )
        return int(cursor.fetchone()[0] or 0)


def _paused_panda_tasks(limit=20):
    """Return the complete paused count plus recent task detail."""
    from django.db import connections
    from ..panda.queries import PANDA_SCHEMA

    connection = connections['panda']
    with connection.cursor() as cursor:
        cursor.execute(
            f'SELECT COUNT(*) FROM "{PANDA_SCHEMA}"."jedi_tasks" '
            'WHERE "status" = %s',
            ['paused'],
        )
        count = int(cursor.fetchone()[0] or 0)
        cursor.execute(
            f'SELECT "jeditaskid", "taskname", "username", '
            f'"modificationtime" FROM "{PANDA_SCHEMA}"."jedi_tasks" '
            'WHERE "status" = %s ORDER BY "modificationtime" DESC '
            'LIMIT %s',
            ['paused', limit],
        )
        tasks = [
            {
                'jedi_task_id': row[0],
                'task_name': row[1] or '',
                'username': row[2] or '',
                'modified_at': row[3].isoformat() if row[3] else None,
            }
            for row in cursor.fetchall()
        ]
    return count, tasks


def _user_testbed_summary(username, now):
    """Return display and detail state for one user's SWF testbed."""
    from ..models import SysConfig, SystemAgent
    from ..workflow_models import Namespace, WorkflowExecution

    healthy_after = now - timedelta(minutes=5)
    stale_hours = float(SysConfig.get_setting('state_stale_hours', 12))
    stale_before = now - timedelta(hours=stale_hours)

    manager = SystemAgent.objects.filter(
        instance_name=f'agent-manager-{username}').first()
    executions = WorkflowExecution.objects.filter(executed_by=username)
    latest_execution = executions.order_by('-start_time').first()

    manager_is_fresh = bool(
        manager and manager.last_heartbeat
        and manager.last_heartbeat >= healthy_after
        and manager.operational_state != 'EXITED')
    owned_namespace = (Namespace.objects.filter(owner=username)
                       .order_by('-updated_at')
                       .values_list('name', flat=True).first())
    namespace = None
    if manager_is_fresh and manager.namespace:
        namespace = manager.namespace
    elif latest_execution and latest_execution.namespace:
        namespace = latest_execution.namespace
    elif manager and manager.namespace:
        namespace = manager.namespace
    else:
        namespace = owned_namespace

    agents = []
    if namespace:
        agents = list(SystemAgent.objects.filter(namespace=namespace)
                      .exclude(instance_name=f'agent-manager-{username}')
                      .exclude(operational_state='EXITED')
                      .order_by('-last_heartbeat'))
    fresh_agents = [
        agent for agent in agents
        if agent.last_heartbeat and agent.last_heartbeat >= healthy_after
    ]
    stale_agents = [agent for agent in agents if agent not in fresh_agents]
    error_agents = [agent for agent in fresh_agents if agent.status == 'ERROR']
    running_workflows = executions.filter(status='running').count()
    stale_workflows = executions.filter(
        status='running', end_time__isnull=True,
        start_time__lt=stale_before).count()

    if stale_workflows:
        color = 'yellow'
    elif error_agents or (manager_is_fresh and manager.status == 'ERROR'):
        color = 'red'
    elif running_workflows:
        color = 'green'
    elif fresh_agents:
        color = 'green'
    else:
        color = None
    display_count = len(fresh_agents)
    label = f'testbed {display_count}'

    return {
        'label': label,
        'color': color,
        'namespace': namespace,
        'agent_manager': {
            'alive': manager_is_fresh,
            'status': manager.status if manager else None,
            'last_heartbeat': (
                manager.last_heartbeat.isoformat()
                if manager and manager.last_heartbeat else None),
        },
        'agents': {
            'display_count': display_count,
            'fresh': len(fresh_agents),
            'stale': len(stale_agents),
            'error': len(error_agents),
        },
        'workflows': {
            'running': running_workflows,
            'stale': stale_workflows,
            'stale_after_hours': stale_hours,
        },
        'last_execution': ({
            'execution_id': latest_execution.execution_id,
            'status': latest_execution.status,
            'start_time': (latest_execution.start_time.isoformat()
                           if latest_execution.start_time else None),
            'end_time': (latest_execution.end_time.isoformat()
                         if latest_execution.end_time else None),
        } if latest_execution else None),
    }


def _user_panda_summary(username):
    """Return a compact trailing-day PanDA summary for one effective user."""
    from ..panda.queries import get_activity

    activity = get_activity(days=1, username=username)
    if activity.get('error'):
        raise RuntimeError(activity['error'])
    jobs = activity.get('jobs') or {}
    tasks = activity.get('tasks') or {}
    job_status = jobs.get('by_status') or {}
    task_status = tasks.get('by_status') or {}

    running_jobs = int(job_status.get('running', 0) or 0)
    terminal_tasks = {'done', 'failed', 'aborted', 'broken', 'finished'}
    active_tasks = sum(
        int(count or 0) for status, count in task_status.items()
        if status not in terminal_tasks)
    finished_jobs = int(job_status.get('finished', 0) or 0)
    failed_jobs = sum(int(job_status.get(status, 0) or 0)
                      for status in ('failed', 'cancelled', 'closed'))

    if running_jobs:
        display_count = running_jobs
    elif active_tasks:
        display_count = active_tasks
    else:
        display_count = finished_jobs + failed_jobs
    label = f'PanDA {display_count}'

    return {
        'label': label,
        'window_hours': 24,
        'display_count': display_count,
        'running_jobs': running_jobs,
        'active_tasks': active_tasks,
        'finished_jobs': finished_jobs,
        'failed_jobs': failed_jobs,
        'jobs_by_status': job_status,
        'tasks_by_status': task_status,
    }


def capcom_state(request):
    """SWF state tiles: the System page verdict and current PanDA activity.

    states: tile-exact entries (source, value, color, url).
    detail: the numbers behind them, for any richer capcom-side use.
    Each section degrades independently: a failing source contributes an
    error tile rather than hiding or failing the whole response.
    """
    from ..snapper_panda import _in_flight_activity, _terminal_outcome_rows
    from ..system_status import status_summary

    now = timezone.now()
    states = []
    detail = {}

    try:
        summary = status_summary(exclude_names=CAPCOM_WORKLOAD_CHECKS)
        status = summary.get('overall_status', 'unknown')
        bad = int(summary.get('warning', 0)) + int(summary.get('error', 0))
        value = status.upper() + (f' ({bad})' if bad else '')
        color = {'ok': 'green', 'warning': 'yellow',
                 'error': 'red'}.get(status)
        entry = {'source': 'swf-system', 'value': value,
                 'url': f'{REMOTE_FACE}/system/'}
        if color:
            entry['color'] = color
        states.append(entry)
        latest = summary.get('latest_checked_at')
        detail['system'] = {
            'scope': 'infrastructure-operations',
            'excluded_checks': sorted(CAPCOM_WORKLOAD_CHECKS),
            'status': status,
            'reason': summary.get('overall_reason', ''),
            'ok': summary.get('ok', 0),
            'warning': summary.get('warning', 0),
            'error': summary.get('error', 0),
            'checked_at': latest.isoformat() if latest else None,
        }
    except Exception as exc:
        logger.error('capcom state: system summary failed: %s', exc)
        states.append({'source': 'swf-system', 'value': 'UNAVAILABLE',
                       'url': f'{REMOTE_FACE}/system/'})
        detail['system'] = {'error_text': str(exc)}

    try:
        running_jobs = sum(
            row['jobs'] for row in _in_flight_activity()
            if row['status'] == 'running')
        finished = 0
        failed = 0
        for _site, status, _cls, count in _terminal_outcome_rows(
                now - timedelta(hours=12), now):
            if status == 'finished':
                finished += int(count or 0)
            elif status == 'failed':
                failed += int(count or 0)
        decided = finished + failed
        pct = round(100.0 * finished / decided, 1) if decided else None
        running_tasks = _running_panda_task_count()
        paused_count, paused_tasks = _paused_panda_tasks()
        value_parts = [f'{running_jobs} jobs', f'{running_tasks} tasks',
                       f'{paused_count} paused']
        if pct is not None:
            value_parts.append(f'{pct:.0f}%')
        entry = {
            'source': 'swf-panda',
            'value': _compact_value(' · '.join(value_parts)),
            'url': f'{REMOTE_FACE}/panda/jobs/',
        }
        if paused_count:
            entry['color'] = 'yellow'
        states.append(entry)
        detail['panda'] = {
            'running_jobs': running_jobs,
            'running_tasks': running_tasks,
            'finished_12h': finished,
            'failed_12h': failed,
            'success_pct_12h': pct,
            'paused_tasks': paused_count,
            'paused_task_detail': paused_tasks,
        }
    except Exception as exc:
        logger.error('capcom state: panda queries failed: %s', exc)
        states.append({'source': 'swf-panda', 'value': 'UNAVAILABLE',
                       'url': f'{REMOTE_FACE}/panda/jobs/'})
        detail['panda'] = {'error_text': str(exc)}

    try:
        from ..alarms_data import active_event_count, alarm_configs

        counts = {}
        for cfg in alarm_configs():
            entry_id = cfg.get('entry_id') or ''
            if entry_id:
                counts[cfg.get('name') or entry_id] = (
                    active_event_count(entry_id))
        active = sum(counts.values())
        states.append({
            'source': 'swf-alarms',
            'value': f'{active} active' if active else 'OK',
            'color': 'red' if active else 'green',
            'url': f'{REMOTE_FACE}/alarms/'})
        detail['alarms'] = {
            'active': active,
            'by_alarm': {name: n for name, n in counts.items() if n}}
    except Exception as exc:
        logger.error('capcom state: alarm counts failed: %s', exc)
        states.append({'source': 'swf-alarms', 'value': 'UNAVAILABLE',
                       'url': f'{REMOTE_FACE}/alarms/'})
        detail['alarms'] = {'error_text': str(exc)}

    try:
        from ..epicprod_logging import SUBLEVEL_VALUES, live_stream_q
        from ..models import AIMemory, AppLog, SysConfig

        since = now - timedelta(hours=24)
        # Posts: what the epicprod-live publisher put on the channel —
        # the same SysConfig-governed selection it publishes from.
        min_sublevel = str(SysConfig.get_setting(
            'epicprod_live_min_sublevel', 'normal') or '')
        if min_sublevel not in SUBLEVEL_VALUES:
            min_sublevel = 'normal'
        posts = AppLog.objects.filter(
            live_stream_q(min_sublevel), timestamp__gte=since).count()
        # Queries: DISpatcher-handled questions (channel, mentions, DMs)
        # — one user-role memory row is recorded per handled exchange.
        queries = AIMemory.objects.filter(
            username='pandabot', session_id='mattermost', role='user',
            created_at__gte=since).count()
        states.append({
            'source': 'swf-bot',
            'value': (f'{posts} post{"s" if posts != 1 else ""} · '
                      f'{queries} quer{"ies" if queries != 1 else "y"}/24h'),
            'url': 'https://chat.epic-eic.org/main/channels/dispatcher'})
        detail['dispatcher'] = {'posts_24h': posts, 'queries_24h': queries,
                                'min_sublevel': min_sublevel}
    except Exception as exc:
        logger.error('capcom state: dispatcher counts failed: %s', exc)
        states.append({
            'source': 'swf-bot', 'value': 'UNAVAILABLE',
            'url': 'https://chat.epic-eic.org/main/channels/dispatcher'})
        detail['dispatcher'] = {'error_text': str(exc)}

    return JsonResponse({'built_at': now.isoformat(),
                         'states': states, 'detail': detail})


def capcom_notices(request):
    """Open read: buffered discrete SWF events after a consumer's cursor.

    Query params:
        since (ISO-8601 timestamp, optional) — return notices created
        strictly after this instant; a naive value is read as UTC.
        Default: the trailing 24 hours.
        subscriber (optional) — the per-subscriber buffer to drain
        (docs/NOTICE_ROUTING.md); default 'capcom', the original
        single consumer.

    Rows come back oldest-first so the consumer's next cursor is the last
    row's created_at; 'more' is true when the page cap truncated the
    response and another read should follow immediately.
    """
    from ..models import CapcomNotice

    now = timezone.now()
    since_raw = (request.GET.get('since') or '').strip()
    if since_raw:
        try:
            since = datetime.fromisoformat(since_raw)
        except ValueError:
            return JsonResponse(
                {'error': 'since must be an ISO-8601 timestamp'}, status=400)
        if timezone.is_naive(since):
            since = since.replace(tzinfo=datetime_timezone.utc)
    else:
        since = now - timedelta(hours=24)

    subscriber = (request.GET.get('subscriber') or 'capcom').strip()[:100]
    rows = list(CapcomNotice.objects.filter(created_at__gt=since,
                                            subscriber=subscriber)
                .order_by('created_at')[:NOTICE_PAGE_MAX + 1])
    more = len(rows) > NOTICE_PAGE_MAX
    rows = rows[:NOTICE_PAGE_MAX]
    return JsonResponse({
        'built_at': now.isoformat(),
        'since': since.isoformat(),
        'more': more,
        'notices': [{
            'created_at': row.created_at.isoformat(),
            'source': row.source,
            'severity': row.severity,
            'title': row.title,
            'detail': row.detail,
            'url': row.url,
            'dedup_key': row.dedup_key,
        } for row in rows],
    })


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def capcom_notice_ingest(request):
    """Token-authenticated notice write from the prod-ops agent."""
    from ..models import CapcomNotice

    source = str(request.data.get('source') or '').strip()
    title = str(request.data.get('title') or '').strip()
    if not source or not title:
        return Response({'error': 'source and title are required'},
                        status=400)
    notice = CapcomNotice.objects.create(
        source=source[:100],
        severity=str(request.data.get('severity') or 'info')[:20],
        title=title[:300],
        detail=str(request.data.get('detail') or ''),
        url=str(request.data.get('url') or '')[:500],
        dedup_key=str(request.data.get('dedup_key') or '')[:200],
    )
    purged, _ = CapcomNotice.objects.filter(
        created_at__lt=timezone.now()
        - timedelta(days=NOTICE_RETENTION_DAYS)).delete()
    if purged:
        logger.info('capcom notices: purged %d expired rows', purged)
    return Response({'status': 'ok', 'id': notice.id})


def capcom_user_state(request):
    """One display-ready SWF tile for a requested user's own activity/state."""
    username = (request.GET.get('username') or '').strip()
    if not USERNAME_RE.fullmatch(username):
        return JsonResponse({
            'error': ('username is required and may contain only letters, '
                      'numbers, dot, underscore, and hyphen'),
        }, status=400)

    now = timezone.now()
    detail = {'username': username}
    color = None
    try:
        testbed = _user_testbed_summary(username, now)
        testbed_label = testbed.pop('label')
        color = testbed.pop('color')
        detail['testbed'] = testbed
    except Exception as exc:
        logger.error('capcom user state: testbed query for %s failed: %s',
                     username, exc)
        testbed_label = 'testbed unavailable'
        color = 'red'
        detail['testbed'] = {'error_text': str(exc)}

    try:
        panda = _user_panda_summary(username)
        panda_label = panda.pop('label')
        detail['panda'] = panda
    except Exception as exc:
        logger.error('capcom user state: PanDA query for %s failed: %s',
                     username, exc)
        panda_label = 'PanDA unavailable'
        color = 'red'
        detail['panda'] = {'error_text': str(exc)}

    entry = {
        'source': 'swf-user',
        'value': f'{testbed_label} · {panda_label}',
        'url': f'{REMOTE_FACE}/panda/jobs/?{urlencode({"days": 1, "username": username})}',
    }
    if color:
        entry['color'] = color
    return JsonResponse({
        'built_at': now.isoformat(),
        'username': username,
        'states': [entry],
        'detail': detail,
    })


class NoticeSubscriptionViewSet(viewsets.ModelViewSet):
    """Consumer-registered notice subscriptions (docs/NOTICE_ROUTING.md).

    Read open; writes token-authenticated. A consumer registers and
    maintains its own subscriptions without swf code changes; every
    change is a logged action.
    """
    queryset = NoticeSubscription.objects.all()
    serializer_class = NoticeSubscriptionSerializer
    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticatedOrReadOnly]
    filterset_fields = ['subscriber', 'event', 'enabled']

    def perform_create(self, serializer):
        obj = serializer.save(
            created_by=getattr(self.request.user, 'username', '') or '')
        self._log('created', obj)

    def perform_update(self, serializer):
        obj = serializer.save()
        self._log('updated', obj)

    def perform_destroy(self, instance):
        self._log('removed', instance)
        instance.delete()

    def _log(self, change, obj):
        from ..epicprod_logging import log_epicprod_action
        log_epicprod_action(
            'web', 'notice_subscription_edit',
            subject_type='notice_subscription',
            subject_key=f'{obj.subscriber}:{obj.event}',
            username=getattr(self.request.user, 'username', '') or '',
            sublevel='normal', live_default=False,
            summary=f'{change}: {obj.subscriber} subscribes to {obj.event}',
            change=change, delivery=obj.delivery, enabled=obj.enabled)
