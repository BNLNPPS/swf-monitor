"""Warn on production service disks before logging or scheduling stops.

The host reporters measure the filesystems; this detector reads their
existing records, with no SSH or credentials in the web tier.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..common import Detection

PARAMS = {
    'hosts': 'osgsub01,pandaharvester01,pandaharvester02,pandaserver01',
    'warn_used_percent': 90,
    'stale_after_minutes': 15,
}


def _stamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def detect(client, params):
    hosts = [h.strip() for h in str(params.get('hosts', PARAMS['hosts'])).split(',') if h.strip()]
    limit = float(params.get('warn_used_percent', 90))
    stale = float(params.get('stale_after_minutes', 15)) * 60
    if not 0 < limit < 100 or stale <= 0:
        raise ValueError('invalid host disk-space thresholds')
    # A failed database read raises: the engine preserves existing alarms.
    with client.db_conn.cursor() as cur:
        cur.execute('SELECT key, value, built_at FROM swf_cached_product WHERE key = ANY(%s)',
                    [['host_report:' + h for h in hosts]])
        reports = {row['key'].split(':', 1)[1]: row for row in cur.fetchall()}
    now = datetime.now(timezone.utc)
    for host in hosts:
        row = reports.get(host)
        record = (row or {}).get('value') or {}
        if isinstance(record, str):
            record = json.loads(record)
        reason = None
        try:
            stamps = [_stamp(row['built_at']), _stamp(record['collected_at'])]
            age = max((now - stamp).total_seconds() for stamp in stamps)
            if age > stale or age < -60:
                reason = 'disk report is stale (age {:.0f} min)'.format(age / 60)
        except (KeyError, TypeError, AttributeError, ValueError):
            reason = 'no valid disk report timestamp'
        volumes = (record.get('health') or {}).get('volumes') or {}
        bad, errors, seen = [], [], set()
        for mount, volume in volumes.items():
            device = volume.get('device', mount)
            if device in seen:
                continue
            seen.add(device)
            used, free = volume.get('used_percent'), volume.get('free_bytes')
            if not isinstance(used, (float, int)) or not isinstance(free, (float, int)):
                errors.append(mount)
            elif used >= limit:
                bad.append(dict(path=mount, used_percent=used, free_bytes=free,
                                total_bytes=volume.get('total_bytes')))
        if not volumes:
            reason = reason or 'disk measurements are missing'
        elif errors:
            reason = reason or 'disk measurements unavailable for ' + ', '.join(errors)
        # One stable incident per host also remains active if monitoring
        # becomes blind; a missing report never counts as recovered space.
        if reason:
            yield Detection('host-disk:' + host, host + ': disk monitoring unavailable',
                            reason + '; disk headroom cannot be confirmed.',
                            {'host': host, 'reason': reason})
        elif bad:
            bad.sort(key=lambda item: -item['used_percent'])
            detail = '; '.join('{} {:.1f}% full, {:.1f} GB free'.format(
                v['path'], v['used_percent'], v['free_bytes'] / 1e9) for v in bad)
            yield Detection('host-disk:' + host, host + ': ' + detail,
                            'Disk-space warning at {:.0f}% used: {}. Free space before '
                            'logs, HTCondor or Harvester can no longer write.'.format(limit, detail),
                            {'host': host, 'volumes': bad, 'warn_used_percent': limit,
                             'collected_at': record['collected_at']})
