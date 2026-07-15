"""Cached system status collection for ePIC production operations."""

import json
import socket
import subprocess
import urllib.error
import urllib.request
from datetime import timedelta

from django.db import OperationalError, ProgrammingError, transaction
from django.utils import timezone

from .models import (SystemAgent, SystemStatus, SystemStatusHistory,
                     external_face_base_url)


HISTORY_MIN_INTERVAL = timedelta(hours=6)
STATUS_STALE_AFTER = timedelta(minutes=15)


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


COLLECTORS = {
    'epicprod-ops-agent': _ops_agent,
    'swf-panda-bot': _panda_bot,
    'swf-monitor-mcp-asgi': lambda: _systemctl_unit(
        'swf-monitor-mcp-asgi', 'swf-monitor-mcp-asgi', category='services'),
    'httpd': lambda: _systemctl_unit('httpd', 'httpd', category='services'),
    'epic-devcloud-prod': lambda: _http_endpoint(
        'epic-devcloud-prod', f'{external_face_base_url()}/prod/'),
    'epic-devcloud-doc': lambda: _http_endpoint(
        'epic-devcloud-doc', f'{external_face_base_url()}/doc/'),
}


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
