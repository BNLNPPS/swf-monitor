"""Cached system status collection for ePIC production operations."""

import base64
import json
import os
import socket
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from django.db import OperationalError, ProgrammingError, transaction
from django.utils import timezone

from .models import (AIMemory, SysConfig, SystemAgent, SystemStatus,
                     SystemStatusHistory, external_face_base_url)


HISTORY_MIN_INTERVAL = timedelta(hours=6)
STATUS_STALE_AFTER = timedelta(minutes=15)

# Repos whose GitHub Actions workflows feed the ci status collector.
GITHUB_REPOS = ['BNLNPPS/swf-monitor', 'BNLNPPS/swf-epicprod',
                'BNLNPPS/swf-testbed', 'BNLNPPS/swf-common-lib']
_GH_FAIL_CONCLUSIONS = {'failure', 'startup_failure', 'timed_out'}


def _status(name, category, status, summary, data=None, checked_at=None):
    return {
        'name': name,
        'category': category,
        'status': status,
        'summary': summary,
        'data': data or {},
        'checked_at': checked_at or timezone.now(),
    }


def _run_checked(cmd, timeout=5):
    started = timezone.now()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            'ok': False,
            'returncode': None,
            'stdout': '',
            'stderr': f'timed out after {timeout}s',
            'elapsed_ms': int((timezone.now() - started).total_seconds() * 1000),
        }
    except OSError as exc:
        return {
            'ok': False,
            'returncode': None,
            'stdout': '',
            'stderr': str(exc),
            'elapsed_ms': int((timezone.now() - started).total_seconds() * 1000),
        }
    return {
        'ok': p.returncode == 0,
        'returncode': p.returncode,
        'stdout': (p.stdout or '').strip(),
        'stderr': (p.stderr or '').strip(),
        'elapsed_ms': int((timezone.now() - started).total_seconds() * 1000),
    }


def _systemctl_unit(name, unit, category='services'):
    active = _run_checked(['systemctl', 'is-active', unit], timeout=5)
    enabled = _run_checked(['systemctl', 'is-enabled', unit], timeout=5)
    show = _run_checked(
        ['systemctl', 'show', unit, '--property=ActiveState,SubState,MainPID,NRestarts'],
        timeout=5,
    )
    fields = {}
    for line in show.get('stdout', '').splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            fields[key] = value

    is_active = active.get('stdout') == 'active'
    if is_active:
        state = 'ok'
    elif active.get('stdout') in {'activating', 'deactivating'}:
        state = 'warning'
    else:
        state = 'error'

    summary = f"{unit} is {active.get('stdout') or 'unknown'}"
    if enabled.get('stdout'):
        summary += f", {enabled['stdout']}"
    return _status(name, category, state, summary, {
        'unit': unit,
        'host': socket.gethostname(),
        'systemctl': {
            'is_active': active,
            'is_enabled': enabled,
            'show': fields,
        },
    })


def _latest_agent_snapshot(filters):
    try:
        agent = SystemAgent.objects.filter(**filters).order_by('-last_heartbeat', '-updated_at').first()
    except Exception as exc:
        return {'lookup_error': str(exc)}
    if not agent:
        return None
    return {
        'instance_name': agent.instance_name,
        'status': agent.status,
        'operational_state': agent.operational_state,
        'last_heartbeat': agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
        'hostname': agent.hostname,
        'pid': agent.pid,
        'namespace': agent.namespace,
        'metadata': agent.metadata or {},
    }


def _ops_agent():
    item = _systemctl_unit('epicprod-ops-agent', 'epicprod-ops-agent', category='agents')
    agent = _latest_agent_snapshot({'namespace': 'prodops', 'agent_type': 'PRODOPS'})
    item['data']['agent_row'] = agent
    if agent:
        item['summary'] += f"; monitor heartbeat {agent.get('status')}/{agent.get('operational_state')}"
        if agent.get('status') not in {'OK', 'WARNING'}:
            item['status'] = 'warning' if item['status'] == 'ok' else item['status']
    elif item['status'] == 'ok':
        item['status'] = 'warning'
        item['summary'] += '; no matching monitor heartbeat row'
    return item


def _panda_bot():
    item = _systemctl_unit('swf-panda-bot', 'swf-panda-bot', category='agents')
    agent = _latest_agent_snapshot({'instance_name__icontains': 'panda'})
    item['data']['agent_row'] = agent
    if agent:
        item['summary'] += f"; monitor heartbeat {agent.get('status')}/{agent.get('operational_state')}"
    return item


def _http_endpoint(name, url):
    started = timezone.now()
    req = urllib.request.Request(url, method='GET', headers={'User-Agent': 'swf-monitor-system-status/1.0'})
    data = {'url': url}
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read(512)
            code = resp.getcode()
            elapsed_ms = int((timezone.now() - started).total_seconds() * 1000)
            data.update({
                'http_status': code,
                'final_url': resp.geturl(),
                'elapsed_ms': elapsed_ms,
                'sample_bytes': len(body),
            })
    except urllib.error.HTTPError as exc:
        elapsed_ms = int((timezone.now() - started).total_seconds() * 1000)
        data.update({
            'http_status': exc.code,
            'final_url': exc.geturl(),
            'elapsed_ms': elapsed_ms,
            'error': str(exc),
        })
        if exc.code >= 500:
            return _status(name, 'external', 'error', f'{url} returned HTTP {exc.code}', data)
        return _status(name, 'external', 'warning', f'{url} returned HTTP {exc.code}', data)
    except Exception as exc:
        elapsed_ms = int((timezone.now() - started).total_seconds() * 1000)
        data.update({'elapsed_ms': elapsed_ms, 'error': str(exc)})
        return _status(name, 'external', 'error', f'{url} check failed: {exc}', data)

    state = 'ok' if data['http_status'] < 400 else 'warning'
    return _status(name, 'external', state, f"{url} returned HTTP {data['http_status']}", data)


def _github_actions():
    """Latest completed run of every workflow in the core repos; a failing
    workflow is an error and reddens the system. Runs only in the agent's
    cached refresh, never in a page render. GITHUB_TOKEN/GH_TOKEN in the
    agent environment raises the API rate limit; unauthenticated suffices
    at the default refresh cadence."""
    started = timezone.now()
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    headers = {'Accept': 'application/vnd.github+json',
               'User-Agent': 'swf-monitor-system-status'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    failing, ok_count, api_errors = [], 0, []
    for repo in GITHUB_REPOS:
        url = (f'https://api.github.com/repos/{repo}/actions/runs'
               f'?status=completed&per_page=30')
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.load(resp)
        except Exception as exc:
            api_errors.append(f'{repo}: {exc}')
            continue
        latest_by_workflow = {}
        for run in payload.get('workflow_runs', []):
            # Only main and the coordinated baselines redden the system;
            # PR-branch failures are the pull request's concern.
            branch = run.get('head_branch') or ''
            if branch != 'main' and not branch.startswith('infra/baseline-'):
                continue
            latest_by_workflow.setdefault(run.get('workflow_id'), run)
        for run in latest_by_workflow.values():
            if run.get('conclusion') in _GH_FAIL_CONCLUSIONS:
                failing.append({
                    'repo': repo,
                    'workflow': run.get('name') or '',
                    'branch': run.get('head_branch') or '',
                    'conclusion': run.get('conclusion') or '',
                    'url': run.get('html_url') or '',
                })
            else:
                ok_count += 1
    data = {'failing': failing, 'ok_workflows': ok_count,
            'repos': GITHUB_REPOS, 'api_errors': api_errors,
            'elapsed_ms': int((timezone.now() - started).total_seconds() * 1000)}
    if failing:
        # Warning, not error: a development CI failure should not redden
        # the collaboration-facing System indicator.
        f = failing[0]
        more = f' (+{len(failing) - 1} more)' if len(failing) > 1 else ''
        return _status('github-actions', 'ci', 'warning',
                       f"{f['repo'].split('/')[-1]} / {f['workflow']} failing"
                       f" on {f['branch']}{more}: {f['url']}", data)
    if api_errors:
        return _status('github-actions', 'ci', 'warning',
                       f'GitHub API unreachable for {len(api_errors)} '
                       f'repo(s): {api_errors[0]}', data)
    return _status('github-actions', 'ci', 'ok',
                   f'latest runs green across {ok_count} workflows in '
                   f'{len(GITHUB_REPOS)} repos', data)


def _bot_usage():
    """Bot conversation volume from the recorded exchanges — informational,
    always ok: channel vs DM user turns over the last 7 and 30 days.
    Aggregate counts only; the page is an open surface, so no per-user
    detail."""
    now = timezone.now()
    base = AIMemory.objects.filter(role='user', session_id='mattermost')

    def split(days):
        qs = base.filter(created_at__gte=now - timedelta(days=days))
        return (qs.filter(content__regex=r'^\[[^\]]+ in #').count(),
                qs.filter(content__regex=r'^\[[^\]]+ in DM\]').count())

    ch7, dm7 = split(7)
    ch30, dm30 = split(30)
    data = {'turns_7d': {'channel': ch7, 'dm': dm7},
            'turns_30d': {'channel': ch30, 'dm': dm30}}
    return _status('bot-usage', 'agents', 'ok',
                   f'bot user turns 7d: {ch7} channel, {dm7} DM; '
                   f'30d: {ch30} channel, {dm30} DM', data)


def _campaign_assessments():
    """Scheduled campaign-assessment slots actually filled — the freshness
    alarm for a run lost upstream of registration (trigger, corun run,
    completion callback, enforcement). Policy lives in
    swf_epicprod.assessment.freshness; a collector exception surfaces as
    a red collector-failed row via refresh_system_status."""
    from swf_epicprod.assessment.freshness import assessment_freshness
    status, summary, data = assessment_freshness()
    return _status('campaign-assessments', 'agents', status, summary, data)


def _snapper_scheduler(scope):
    """Assess one PostgreSQL-backed Snapper capture cursor."""
    from snapper_ai.models import CaptureCursor

    opportunity_key = f'snapper_opportunity_seconds_{scope}'
    baseline_key = f'snapper_baseline_every_{scope}'
    opportunity = SysConfig.get_setting(opportunity_key, 10)
    baseline = SysConfig.get_setting(baseline_key, 10)
    try:
        opportunity = int(opportunity)
        baseline = int(baseline)
        if opportunity < 10 or baseline < 1:
            raise ValueError
    except (TypeError, ValueError):
        return _status(
            f'snapper-{scope}-scheduler', 'agents', 'error',
            f'Snapper {scope} scheduler configuration is invalid.',
            {
                'scope': scope,
                opportunity_key: opportunity,
                baseline_key: baseline,
            },
        )

    cursor = CaptureCursor.objects.filter(scope=scope).first()
    if cursor is None:
        return _status(
            f'snapper-{scope}-scheduler', 'agents', 'unknown',
            f'Snapper {scope} scheduler has no capture cursor.',
            {
                'scope': scope,
                'opportunity_seconds': opportunity,
                'baseline_every': baseline,
            },
        )
    now = timezone.now()
    heartbeat_limit = timedelta(seconds=max(3 * opportunity, 60))
    heartbeat_age = (
        (now - cursor.heartbeat_at).total_seconds()
        if cursor.heartbeat_at else None
    )
    data = {
        'scope': scope,
        'opportunity_seconds': opportunity,
        'baseline_every': baseline,
        'heartbeat_at': (
            cursor.heartbeat_at.isoformat() if cursor.heartbeat_at else None),
        'heartbeat_age_seconds': heartbeat_age,
        'heartbeat_limit_seconds': int(heartbeat_limit.total_seconds()),
        'latest_boundary_at': (
            cursor.latest_boundary_at.isoformat()
            if cursor.latest_boundary_at else None),
        'latest_check_at': (
            cursor.latest_check_at.isoformat()
            if cursor.latest_check_at else None),
        'latest_snap_id': (
            str(cursor.latest_snap_id) if cursor.latest_snap_id else None),
        'baseline_progress': cursor.baseline_progress,
        'consecutive_failures': cursor.consecutive_failures,
        'coverage_gap_started_at': (
            cursor.coverage_gap_started_at.isoformat()
            if cursor.coverage_gap_started_at else None),
        'scheduler_result': cursor.scheduler_result or {},
    }
    if cursor.consecutive_failures:
        return _status(
            f'snapper-{scope}-scheduler', 'agents', 'error',
            f'Snapper {scope} scheduler has consecutive capture failures.', data)
    if cursor.coverage_gap_started_at:
        return _status(
            f'snapper-{scope}-scheduler', 'agents', 'error',
            f'Snapper {scope} scheduler has an open coverage gap.', data)
    if heartbeat_age is None or heartbeat_age > heartbeat_limit.total_seconds():
        return _status(
            f'snapper-{scope}-scheduler', 'agents', 'error',
            f'Snapper {scope} scheduler heartbeat is stale.', data)
    outcome = (cursor.scheduler_result or {}).get('outcome') or 'unknown'
    if outcome == 'failed':
        return _status(
            f'snapper-{scope}-scheduler', 'agents', 'error',
            f'Snapper {scope} scheduler latest capture failed.', data)
    return _status(
        f'snapper-{scope}-scheduler', 'agents', 'ok',
        f'Snapper {scope} scheduler is evaluating boundaries; '
        f'latest outcome is {outcome}.', data)


def _activemq_broker():
    """The message broker every agent, bot, and fast-processing worker
    depends on. Three evidence sources: STOMP-TLS reachability measured as
    a real timed handshake (the exact operation the acceptor's handshake
    limit cuts for clients), handshake-drop volume from the broker log,
    and broker internals over the console's Jolokia endpoint where the
    console role configuration admits us — until then that source reports
    itself unavailable rather than failing the collector."""
    heap_warn = float(SysConfig.get_setting('activemq_heap_warn_pct', 90))
    drops_warn = int(SysConfig.get_setting('activemq_drops_warn_24h', 200))

    host = os.environ.get('ACTIVEMQ_HOST', 'localhost')
    port = int(os.environ.get('ACTIVEMQ_PORT', '61612'))
    use_ssl = os.environ.get('ACTIVEMQ_USE_SSL', 'False').lower() == 'true'
    data = {'host': host, 'port': port, 'ssl': use_ssl}
    problems = []

    started = timezone.now()
    try:
        with socket.create_connection((host, port), timeout=12) as sock:
            if use_ssl:
                ctx = ssl.create_default_context(
                    cafile=os.environ.get('ACTIVEMQ_SSL_CA_CERTS') or None)
                with ctx.wrap_socket(sock, server_hostname=host):
                    pass
        data['handshake_ms'] = int(
            (timezone.now() - started).total_seconds() * 1000)
    except Exception as exc:
        data['error'] = str(exc)
        return _status('activemq-broker', 'services', 'error',
                       f'broker unreachable at {host}:{port}: {exc}', data)

    # Handshake drops over the trailing 24h from the broker log (Artemis
    # AMQ224088, world-readable): today's file plus yesterday's rollover.
    # Timestamps in the log are machine-local, so compare in local time.
    log_path = os.environ.get('ACTIVEMQ_BROKER_LOG',
                              '/var/lib/swfbroker/log/artemis.log')
    cutoff = datetime.now() - timedelta(hours=24)
    cutoff_s = cutoff.strftime('%Y-%m-%d %H:%M:%S')
    drops = 0
    for path in (f'{log_path}.{cutoff.date().isoformat()}', log_path):
        try:
            with open(path, encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    if 'AMQ224088' in line and line[:19] >= cutoff_s:
                        drops += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            problems.append(f'broker log unreadable: {exc}')
    data['handshake_drops_24h'] = drops

    # Broker internals via the console's Jolokia endpoint. Pattern-form
    # POST reads avoid hardcoding the broker's configured name. The
    # console URL must say "localhost", not 127.0.0.1: Jolokia's strict
    # CORS policy (jolokia-access.xml) admits only *://localhost*
    # origins, and the Origin header must match the request host.
    console = os.environ.get(
        'ACTIVEMQ_CONSOLE_URL', 'http://localhost:8161/console').rstrip('/')
    console_parts = urllib.parse.urlsplit(console)
    origin = f'{console_parts.scheme}://{console_parts.netloc}'
    auth = base64.b64encode(
        f"{os.environ.get('ACTIVEMQ_USER', '')}:"
        f"{os.environ.get('ACTIVEMQ_PASSWORD', '')}".encode()).decode()

    def jolokia(mbean, attributes):
        req = urllib.request.Request(
            f'{console}/jolokia/',
            data=json.dumps({'type': 'read', 'mbean': mbean,
                             'attribute': attributes}).encode(),
            headers={'Content-Type': 'application/json',
                     'Authorization': f'Basic {auth}',
                     'Origin': origin})
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.load(resp)
        if payload.get('status') != 200:
            raise RuntimeError(
                f"jolokia {payload.get('status')} for {mbean}: "
                f"{str(payload.get('error') or '')[:120]}")
        return payload.get('value') or {}

    def first_pattern_value(value):
        for entry in value.values():
            if isinstance(entry, dict):
                return entry
        return {}

    console_ok = False
    heap_pct = connections = messages = dlq = None
    try:
        heap = jolokia('java.lang:type=Memory',
                       ['HeapMemoryUsage'])['HeapMemoryUsage']
        heap_pct = round(100.0 * heap['used'] / heap['max'], 1)
        broker = first_pattern_value(jolokia(
            'org.apache.activemq.artemis:broker=*',
            ['ConnectionCount', 'TotalMessageCount']))
        connections = broker.get('ConnectionCount')
        messages = broker.get('TotalMessageCount')
        dlq_reply = jolokia(
            'org.apache.activemq.artemis:broker=*,component=addresses,'
            'address="DLQ",subcomponent=queues,*', ['MessageCount'])
        dlq = sum(int((entry or {}).get('MessageCount') or 0)
                  for entry in dlq_reply.values() if isinstance(entry, dict))
        console_ok = True
        data.update({'heap_pct': heap_pct, 'heap': heap,
                     'connections': connections, 'messages': messages,
                     'dlq_depth': dlq})
    except Exception as exc:
        problems.append(f'console unavailable: {exc}')
    data['console_available'] = console_ok
    if problems:
        data['problems'] = problems

    state = 'ok'
    parts = [f"broker up ({data['handshake_ms']} ms handshake)"]
    if console_ok:
        parts.append(f'{connections} connections')
        parts.append(f'heap {heap_pct:.0f}%')
        if heap_pct >= heap_warn:
            state = 'warning'
        parts.append('DLQ empty' if not dlq else f'DLQ {dlq}')
        if dlq:
            state = 'warning'
    else:
        parts.append('console unavailable')
    parts.append(f'{drops} handshake drops 24h')
    if drops >= drops_warn:
        state = 'warning'
    return _status('activemq-broker', 'services', state,
                   ' · '.join(parts), data)


COLLECTORS = {
    'epicprod-ops-agent': _ops_agent,
    'activemq-broker': _activemq_broker,
    'swf-panda-bot': _panda_bot,
    'campaign-assessments': _campaign_assessments,
    'swf-monitor-mcp-asgi': lambda: _systemctl_unit(
        'swf-monitor-mcp-asgi', 'swf-monitor-mcp-asgi', category='services'),
    'httpd': lambda: _systemctl_unit('httpd', 'httpd', category='services'),
    'epic-devcloud-prod': lambda: _http_endpoint(
        'epic-devcloud-prod', f'{external_face_base_url()}/prod/'),
    'epic-devcloud-doc': lambda: _http_endpoint(
        'epic-devcloud-doc', f'{external_face_base_url()}/doc/'),
    'github-actions': _github_actions,
    'bot-usage': _bot_usage,
    'snapper-testbed-scheduler': lambda: _snapper_scheduler('testbed'),
    'snapper-epicprod-scheduler': lambda: _snapper_scheduler('epicprod'),
}


# Checks where a human-triggered re-probe is meaningful: the collector
# reads recoverable external truth, so a retry can genuinely clear a red.
# Freshness bookkeeping (campaign-assessments moves only when an
# assessment registers) and informational rows (bot-usage) are excluded —
# re-checking cannot change them.
RETRYABLE_CHECKS = frozenset(COLLECTORS) - {'campaign-assessments',
                                            'bot-usage'}


def _should_append_history(old, new):
    if old is None:
        return True
    if old.status != new['status'] or old.summary != new['summary']:
        return True
    if not old.checked_at:
        return True
    return new['checked_at'] - old.checked_at >= HISTORY_MIN_INTERVAL


@transaction.atomic
def _save_status(item, source='unknown'):
    data = dict(item.get('data') or {})
    data['refresh_source'] = source
    item = dict(item, data=data)
    old = SystemStatus.objects.select_for_update().filter(name=item['name']).first()
    append_history = _should_append_history(old, item)
    obj, _ = SystemStatus.objects.update_or_create(
        name=item['name'],
        defaults={
            'category': item['category'],
            'status': item['status'],
            'summary': item['summary'],
            'data': item['data'],
            'checked_at': item['checked_at'],
        },
    )
    if append_history:
        SystemStatusHistory.objects.create(
            name=item['name'],
            category=item['category'],
            status=item['status'],
            summary=item['summary'],
            data=item['data'],
            checked_at=item['checked_at'],
        )
    return obj


def refresh_system_status(selected=None, source='unknown'):
    """Run selected collectors and update current/history status rows."""
    names = list(selected or COLLECTORS.keys())
    results = []
    for name in names:
        collector = COLLECTORS.get(name)
        if collector is None:
            item = _status(name, 'unknown', 'unknown', f'No collector named {name}', {
                'available_collectors': sorted(COLLECTORS),
            })
        else:
            try:
                item = collector()
            except Exception as exc:
                item = _status(name, 'unknown', 'error', f'Collector failed: {exc}', {
                    'collector': name,
                    'error': str(exc),
                })
        results.append(_save_status(item, source=source))
    return results


def grouped_current_status():
    """Return current rows grouped for the System page, tolerating pre-migration DBs."""
    try:
        rows = list(SystemStatus.objects.order_by('category', 'name'))
    except (OperationalError, ProgrammingError):
        return []
    groups = []
    by_category = {}
    for row in rows:
        by_category.setdefault(row.category, []).append(row)
    for category, items in by_category.items():
        groups.append({'category': category, 'items': items})
    return groups


def status_summary():
    now = timezone.now()
    try:
        rows = list(SystemStatus.objects.all())
    except (OperationalError, ProgrammingError):
        return {
            'ok': 0,
            'warning': 0,
            'error': 0,
            'unknown': 0,
            'total': 0,
            'latest_checked_at': None,
            'oldest_checked_at': None,
            'overall_status': 'unknown',
            'overall_reason': 'System status tables are not available yet.',
        }
    counts = {'ok': 0, 'warning': 0, 'error': 0, 'unknown': 0}
    checked = []
    for row in rows:
        counts[row.status if row.status in counts else 'unknown'] += 1
        if row.checked_at:
            checked.append(row.checked_at)
    counts['total'] = len(rows)
    counts['latest_checked_at'] = max(checked) if checked else None
    counts['oldest_checked_at'] = min(checked) if checked else None
    if not rows:
        counts['overall_status'] = 'unknown'
        counts['overall_reason'] = 'No system status has been collected yet.'
    elif counts['error']:
        counts['overall_status'] = 'error'
        counts['overall_reason'] = f"{counts['error']} check(s) are red."
    elif counts['latest_checked_at'] and now - counts['latest_checked_at'] > STATUS_STALE_AFTER:
        counts['overall_status'] = 'error'
        counts['overall_reason'] = (
            f"System status is stale by more than {int(STATUS_STALE_AFTER.total_seconds() // 60)} minutes."
        )
    elif counts['warning'] or counts['unknown']:
        counts['overall_status'] = 'warning'
        counts['overall_reason'] = 'One or more checks are warning or unknown.'
    else:
        counts['overall_status'] = 'ok'
        counts['overall_reason'] = 'All current checks are OK.'
    return counts


def compact_refresh_report(rows):
    return json.dumps([
        {
            'name': row.name,
            'category': row.category,
            'status': row.status,
            'summary': row.summary,
            'checked_at': row.checked_at.isoformat() if row.checked_at else None,
        }
        for row in rows
    ], indent=2, sort_keys=True)
