"""The declared record: downtime and status declared for the ePIC queues,
storage endpoints and sites, as read from CRIC (swf-epicprod
docs/CONTINUOUS_PRODUCTION.md, Declared downtime).

Three CRIC records carry declared state: PanDA queue status rules
(``pandaqueuestatus``: OFFLINE, BROKEROFF or TEST per queue and activity,
with an expiration, a reason and who set it), DDM endpoint status rules
(``ddmendpointstatus``: the same per endpoint and activity) and downtime
objects (``downtime``: a window with a start, an end, a severity and the
services it takes down). The collector reads the three with the
production proxy and keeps one entry per rule or window in the entry
store (kind ``declared``, context ``cric``), named by its source
identity, with a standing: active, future, expired or cleared. Every
change is a version of the entry; the sync's action names what appeared,
expired and cleared.

The readers here are pure: ``rules_from_pandaqueuestatus``,
``rules_from_ddmendpointstatus`` and ``windows_from_downtime`` turn the
CRIC documents into records, ``standing_of`` places a record in time,
``sync`` writes the store, ``declared_for`` and ``history_for`` serve the
pages and the readers (attribution, node guard, canary, the front).
"""
import logging
from datetime import datetime, timedelta, timezone as dt_timezone

logger = logging.getLogger(__name__)

DECLARED_KIND = 'declared'
DECLARED_CONTEXT = 'cric'
STANDINGS = ('active', 'future', 'expired', 'cleared')
# The pilot reads CRIC's cached queuedata, which lags a rule's expiration
# by minutes: a job that dies on "specified queue is OFFLINE" this long
# after a rule's end is still the rule's.
CACHE_LAG = timedelta(minutes=10)

CRIC_BASE = 'https://datalake-cric.cern.ch/api'
CRIC_QUEUE_STATUS = f'{CRIC_BASE}/atlas/pandaqueuestatus/query/?json&showall=1'
CRIC_ENDPOINT_STATUS = f'{CRIC_BASE}/atlas/ddmendpointstatus/query/?json&showall=1'
CRIC_DOWNTIME = f'{CRIC_BASE}/core/downtime/query/?json'
CRIC_QUEUES = f'{CRIC_BASE}/atlas/pandaqueue/query/?json&vo_name=eic'


def _dt(text):
    """A CRIC timestamp (naive UTC, '2026-09-14T23:21:00' or with
    microseconds) as an aware datetime; None for nothing."""
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt


def _iso(dt):
    return dt.isoformat(timespec='seconds') if dt else None


# ------------------------------------------------------------- the readers

def rules_from_pandaqueuestatus(doc, queues):
    """The status rules of ``queues`` in a ``pandaqueuestatus`` document:
    ``{queue: {activity: {'status': {...}, 'mode': {VALUE: {probe: {...}}}}}}``.
    One record per queue, activity, mode and probe; the source identity
    is that tuple, so a rule re-set with a new expiration is the same
    entry with a new version."""
    out = []
    for queue, activities in (doc or {}).items():
        if queue not in queues:
            continue
        for activity, rec in (activities or {}).items():
            modes = (rec or {}).get('mode') or {}
            for value, probes in modes.items():
                for probe, r in (probes or {}).items():
                    out.append({
                        'name': f'queue:{queue}:{activity}:{value}:{probe}',
                        'kind': 'queue', 'target': queue,
                        'source': 'cric:pandaqueuestatus',
                        'value': value, 'activity': activity, 'probe': probe,
                        'reason': (r or {}).get('reason') or '',
                        'declared_by': (r or {}).get('operationdn') or '',
                        'declared_at': _iso(_dt((r or {}).get('updated'))),
                        'start': None,
                        'end': _iso(_dt((r or {}).get('expiration'))),
                        'info_url': '',
                        'severity': '', 'classification': '', 'description': '',
                    })
    return out


def rules_from_ddmendpointstatus(doc, endpoints):
    """The status rules of ``endpoints`` in a ``ddmendpointstatus``
    document, the same shape as the queue rules."""
    out = []
    for endpoint, activities in (doc or {}).items():
        if endpoint not in endpoints:
            continue
        for activity, rec in (activities or {}).items():
            modes = (rec or {}).get('mode') or {}
            for value, probes in modes.items():
                for probe, r in (probes or {}).items():
                    out.append({
                        'name': f'endpoint:{endpoint}:{activity}:{value}:{probe}',
                        'kind': 'endpoint', 'target': endpoint,
                        'source': 'cric:ddmendpointstatus',
                        'value': value, 'activity': activity, 'probe': probe,
                        'reason': (r or {}).get('reason') or '',
                        'declared_by': (r or {}).get('operationdn') or '',
                        'declared_at': _iso(_dt((r or {}).get('updated'))),
                        'start': None,
                        'end': _iso(_dt((r or {}).get('expiration'))),
                        'info_url': '',
                        'severity': '', 'classification': '', 'description': '',
                    })
    return out


def windows_from_downtime(doc, rcsites):
    """The downtime windows of ``rcsites`` in a ``downtime`` document (a
    dict or list of downtime objects). One record per downtime, on the
    resource centre; the affected services ride along. ``rcsites`` is a
    set of names, or a mapping of name to the queues behind it, which
    the record then carries as ``queues`` so a queue's readers find the
    site's windows."""
    items = list(doc.values()) if isinstance(doc, dict) else list(doc or [])
    queues_of = rcsites if isinstance(rcsites, dict) else {}
    out = []
    for d in items:
        if not isinstance(d, dict):
            continue
        rc = d.get('rc_site') or ''
        if rc not in rcsites:
            continue
        ident = d.get('id') or d.get('pid') or f"{rc}:{d.get('start_time')}"
        out.append({
            'name': f'site:{rc}:{ident}',
            'kind': 'site', 'target': rc,
            'queues': sorted(queues_of.get(rc) or []),
            'source': 'cric:downtime',
            'value': str(d.get('severity') or 'OUTAGE').upper(),
            'activity': '', 'probe': d.get('provider') or '',
            'reason': d.get('description') or '',
            'declared_by': d.get('provider') or '',
            'declared_at': _iso(_dt(d.get('create_time'))),
            'start': _iso(_dt(d.get('start_time'))),
            'end': _iso(_dt(d.get('end_time'))),
            'info_url': d.get('info_url') or '',
            'severity': str(d.get('severity') or ''),
            'classification': str(d.get('classification') or ''),
            'description': d.get('description') or '',
            'services': [s if isinstance(s, str) else (s or {}).get('name', '')
                         for s in (d.get('affected_services') or d.get('services') or [])],
        })
    return out


def standing_of(record, now):
    """Where a declared record stands at ``now``: future (a start not yet
    reached), active (in force), expired (its end passed). A rule with no
    start acts from the moment it is set; a record with no end holds
    until CRIC drops it (cleared, which only the sync can say)."""
    start = _dt(record.get('start'))
    end = _dt(record.get('end'))
    if start and now < start:
        return 'future'
    if end and now >= end:
        return 'expired'
    return 'active'


def _concerns(record, kind, target):
    """Whether a record is about ``target`` of ``kind``: its own target,
    or, for a queue, a site window whose ``queues`` name it."""
    if kind and record.get('kind') != kind:
        if not (kind == 'queue' and record.get('kind') == 'site'
                and target and target in (record.get('queues') or [])):
            return False
        return True
    if target and record.get('target') != target:
        return False
    return True


def declared_at(records, when, kind=None, target=None, lag=CACHE_LAG):
    """The records that covered ``when`` (an aware datetime): in force
    then, or ended within ``lag`` before it. Pure; for the attribution
    of a failure to a declaration."""
    out = []
    for r in records:
        if not _concerns(r, kind, target):
            continue
        start = _dt(r.get('start')) or _dt(r.get('declared_at'))
        end = _dt(r.get('end'))
        cleared = _dt(r.get('cleared_at'))
        stop = min(t for t in (end, cleared) if t) if (end or cleared) else None
        if start and when < start:
            continue
        if stop and when >= stop + lag:
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------- the store

def _context():
    from .models import EntryContext
    ctx, _ = EntryContext.objects.get_or_create(
        name=DECLARED_CONTEXT,
        defaults={'title': 'Declared state from CRIC',
                  'description': 'Downtime and status declared for the ePIC queues, '
                                 'endpoints and sites, one entry per CRIC rule or '
                                 'window (CONTINUOUS_PRODUCTION.md, Declared downtime).'})
    return ctx


def entries():
    """Every declared entry as stored, not archived."""
    from .models import Entry
    return list(Entry.objects.filter(kind=DECLARED_KIND, context_id=DECLARED_CONTEXT,
                                     archived=False, deleted_at__isnull=True))


def records():
    """The declared records: each entry's data with its standing and name."""
    out = []
    for e in entries():
        d = dict(e.data or {})
        d['name'] = e.name
        d['standing'] = e.status or ''
        out.append(d)
    return out


_RECORDS_TTL_S = 60.0
_cache = {'records': None, 'at': 0.0}


def cached_records():
    """The declared records, read at most once a minute: the job error
    root and the job lists read them per job. A failed read logs and
    serves the last copy (or nothing), never raises into a page."""
    import time as time_mod
    now = time_mod.monotonic()
    if _cache['records'] is None or now - _cache['at'] > _RECORDS_TTL_S:
        try:
            _cache['records'] = records()
            _cache['at'] = now
        except Exception as e:                                # noqa: BLE001
            logger.error('declared records read failed: %s', e)
            return _cache['records'] or []
    return _cache['records']


# ------------------------------------------------------ the record's readers

def job_declared(computingsite, endtime):
    """The declaration a failed job fell under, for the per-job error
    root: the queue's rule or its site's window in force at the job's
    end, or ended within CACHE_LAG before it (the pilot's cached
    queuedata lags a rule's expiration). Returns ``{'label', 'line',
    'queue', 'record', 'grade'}`` or None."""
    when = _dt(endtime)
    if not computingsite or when is None:
        return None
    hits = declared_at(cached_records(), when, kind='queue', target=str(computingsite))
    if not hits:
        return None
    r = hits[0]
    return {'label': 'declared downtime', 'line': summary_line(r),
            'queue': str(computingsite), 'record': r.get('name', ''),
            'grade': 'declared record (CRIC)'}


def windows_between(after, before, kind='queue', lag=CACHE_LAG):
    """The (target, start, stop) spans of ``kind`` that overlap
    [after, before): a rule from its declared instant to its end or
    clearing plus the lag, a window from its start; for the summary's
    count of failures under declaration. Site windows are given per
    queue."""
    out = []
    for r in cached_records():
        targets = []
        if r.get('kind') == kind:
            targets = [r['target']]
        elif kind == 'queue' and r.get('kind') == 'site':
            targets = list(r.get('queues') or [])
        if not targets:
            continue
        start = _dt(r.get('start')) or _dt(r.get('declared_at'))
        end, cleared = _dt(r.get('end')), _dt(r.get('cleared_at'))
        stop = min(t for t in (end, cleared) if t) if (end or cleared) else None
        stop = stop + lag if stop else None
        if start is None:
            continue
        if before is not None and start >= before:
            continue
        if stop is not None and after is not None and stop <= after:
            continue
        for target in targets:
            out.append((target, start, stop, summary_line(r)))
    return out


def gate_for_queue(queue, horizon_h, now=None):
    """The front's declared gate for one queue: red with the rule in
    force, or a window starting within ``horizon_h`` hours; the reason is
    the record's line. Pure over the cached records."""
    from django.utils import timezone
    now = now or timezone.now()
    horizon = now + timedelta(hours=float(horizon_h or 0))
    in_force, coming = [], []
    for r in cached_records():
        if r.get('standing') == 'cleared' or not _concerns(r, 'queue', queue):
            continue
        standing = standing_of(r, now)
        if standing == 'active':
            in_force.append(r)
        elif standing == 'future':
            start = _dt(r.get('start'))
            if start and start <= horizon:
                coming.append(r)
    in_force.sort(key=lambda r: r.get('end') or '')
    coming.sort(key=lambda r: r.get('start') or '')
    if in_force:
        return {'red': True, 'state': 'in_force', 'reason': summary_line(in_force[0])}
    if coming:
        return {'red': True, 'state': 'coming', 'reason': summary_line(coming[0])}
    return {'red': False, 'state': '', 'reason': ''}


def sync(found, now, changed_by='cric_declared_state'):
    """Write what CRIC holds now onto the store: a record seen for the
    first time is created with its standing; a known one takes its new
    fields and standing; a record CRIC no longer returns is ``cleared``
    (with the instant) if it was in force or future, ``expired`` if its
    end had passed. Returns the changes: appeared, changed, expired,
    cleared, unchanged (counts), and the names in each."""
    import time as time_mod
    from django.db import transaction
    from .models import Entry
    from .signals import set_changed_by
    set_changed_by(changed_by)
    changes = {'appeared': [], 'changed': [], 'expired': [], 'cleared': [], 'unchanged': 0}
    with transaction.atomic():
        ctx = _context()
        known = {e.name: e for e in Entry.objects.select_for_update().filter(
            kind=DECLARED_KIND, context=ctx, archived=False, deleted_at__isnull=True)}
        seen = set()
        for r in found:
            name = r['name']
            seen.add(name)
            standing = standing_of(r, now)
            e = known.get(name)
            data = {k: v for k, v in r.items() if k != 'name'}
            data['observed_at'] = _iso(now)
            if e is None:
                data['first_seen'] = _iso(now)
                e = Entry(kind=DECLARED_KIND, context=ctx, name=name, status=standing,
                          title=summary_line(r), data=data)
                e.save()
                changes['appeared'].append(name)
                continue
            before = dict(e.data or {})
            data['first_seen'] = before.get('first_seen') or _iso(now)
            if before.get('cleared_at'):
                # Back in CRIC after it had gone: a new life of the same rule.
                data.pop('cleared_at', None)
            comparable = {k: v for k, v in data.items() if k not in ('observed_at',)}
            before_comparable = {k: v for k, v in before.items() if k not in ('observed_at',)}
            if comparable == before_comparable and (e.status or '') == standing:
                changes['unchanged'] += 1
                continue
            if standing == 'expired' and (e.status or '') != 'expired':
                changes['expired'].append(name)
            else:
                changes['changed'].append(name)
            e.data = data
            e.status = standing
            e.title = summary_line(r)
            e.timestamp_modified = time_mod.time()
            e.save()
        for name, e in known.items():
            if name in seen or (e.status or '') in ('expired', 'cleared'):
                continue
            data = dict(e.data or {})
            end = _dt(data.get('end'))
            if end and now >= end:
                e.status = 'expired'
                changes['expired'].append(name)
            else:
                e.status = 'cleared'
                data['cleared_at'] = _iso(now)
                changes['cleared'].append(name)
            e.data = data
            e.timestamp_modified = time_mod.time()
            e.save()
    return changes


# ------------------------------------------------------------- the readers

def _fmt_when(text):
    dt = _dt(text)
    return dt.strftime('%m/%d %H:%M UTC') if dt else ''


def summary_line(r):
    """One line for a queue line or an endpoint line: the rule or window
    as an operator reads it. 'offline until 09/15 00:21 UTC: scheduled
    downtime (xzhao@bnl.gov)'; 'downtime 09/20 08:00 to 16:00 UTC:
    <description>'."""
    who = r.get('declared_by') or ''
    reason = (r.get('reason') or r.get('description') or '').strip()
    tail = (f': {reason}' if reason else '') + (f' ({who})' if who else '')
    if r.get('kind') == 'site':
        span = _fmt_when(r.get('start'))
        if r.get('end'):
            span += f" to {_fmt_when(r.get('end'))}"
        return f"{(r.get('value') or 'downtime').lower()} {span}{tail}".strip()
    value = (r.get('value') or '').lower()
    activity = r.get('activity') or ''
    act = f" ({activity})" if activity and activity != 'a' else ''
    until = f" until {_fmt_when(r.get('end'))}" if r.get('end') else ''
    return f"{value}{act}{until}{tail}".strip()


def declared_for(kind, now=None):
    """What stands declared per target of ``kind`` (queue, endpoint,
    site) at ``now``: ``{target: {'active': [records], 'future':
    [records], 'line': text}}`` with the line the list pages show: the
    rule in force, else the next future window, else ''. The standing is
    recomputed at read time so a rule that expired between two syncs
    reads expired."""
    from django.utils import timezone
    now = now or timezone.now()
    out = {}
    for r in cached_records():
        if r.get('standing') == 'cleared':
            continue
        if r.get('kind') == kind:
            targets = [r['target']]
        elif kind == 'queue' and r.get('kind') == 'site':
            # A site's window is every queue's behind it.
            targets = list(r.get('queues') or [])
        else:
            continue
        standing = standing_of(r, now)
        if standing == 'expired':
            continue
        for target in targets:
            slot = out.setdefault(target, {'active': [], 'future': [], 'line': ''})
            slot[standing].append(r)
    for target, slot in out.items():
        slot['active'].sort(key=lambda r: r.get('end') or '')
        slot['future'].sort(key=lambda r: r.get('start') or '')
        first = (slot['active'] or slot['future'] or [None])[0]
        slot['line'] = summary_line(first) if first else ''
    return out


def history_for(kind, target, limit=20):
    """Every declared record of one target, newest first by its declared
    instant, with standing; for the detail pages."""
    rows = [r for r in records() if r.get('kind') == kind and r.get('target') == target]
    rows.sort(key=lambda r: r.get('declared_at') or r.get('start') or '', reverse=True)
    return rows[:limit]


STANDING_CLASS = {'active': 'offline_fill', 'future': 'pending_fill'}


def for_detail_page(kind, target, now=None):
    """What a detail page shows for one target: ``now`` (the active and
    future records with their lines) and ``history`` (every record, with
    its line, its declared instant and its standing as the page words
    them). One read of the store."""
    from django.utils import timezone
    now = now or timezone.now()
    current = {'active': [], 'future': [], 'line': ''}
    history = []
    for r in history_for(kind, target, limit=50):
        r = dict(r)
        r['line'] = summary_line(r)
        r['when'] = _fmt_when(r.get('declared_at') or r.get('start'))
        r['cleared_when'] = _fmt_when(r.get('cleared_at')) if r.get('cleared_at') else ''
        standing = r.get('standing') if r.get('standing') == 'cleared' else standing_of(r, now)
        r['standing'] = standing
        r['standing_class'] = STANDING_CLASS.get(standing, '')
        history.append(r)
        if standing in ('active', 'future'):
            current[standing].append(r)
    current['active'].sort(key=lambda r: r.get('end') or '')
    current['future'].sort(key=lambda r: r.get('start') or '')
    first = (current['active'] or current['future'] or [None])[0]
    current['line'] = first['line'] if first else ''
    return current, history
