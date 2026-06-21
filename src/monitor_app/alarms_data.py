"""Query helpers for the alarm dashboard — ORM over the Entry / EntryVersion
tables. No model logic beyond what the views need.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta

from django.db.models import Count, Max

from .models import Entry, EntryContext, EntryVersion


CONTEXT_NAME = 'swf-alarms'
TEAMS_CONTEXT = 'teams'


def _active_alarm_configs_qs():
    return (Entry.objects
            .filter(context_id=CONTEXT_NAME, kind='alarm',
                    archived=False, deleted_at__isnull=True)
            .order_by('timestamp_created'))


def alarm_configs() -> list[dict]:
    out = []
    for e in _active_alarm_configs_qs():
        data = e.data or {}
        out.append({
            'id': e.id,
            'entry_id': data.get('entry_id', ''),
            'name': data.get('entry_id', '').replace('alarm_', '', 1) or e.id[:8],
            'title': e.title,
            'content': e.content,
            'enabled': bool(data.get('enabled', True)),
            # Render recipients as a plain string for display. Storage
            # may be str (new-style, user-typed, preserved) or list[str]
            # (legacy seed rows). NEVER `list(x)` on an unknown-typed x —
            # that iterates characters when x is already a string.
            'recipients_text': _recipients_display(data.get('recipients')),
            'params': dict(data.get('params') or {}),
            'created': e.timestamp_created,
            'modified': e.timestamp_modified,
            'data': data,
        })
    return out


def _recipients_display(value) -> str:
    if value is None or value == '':
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ', '.join(str(v) for v in value)
    return str(value)


def get_alarm_config_by_entry_id(entry_id: str) -> Entry | None:
    try:
        return (Entry.objects
                .filter(context_id=CONTEXT_NAME, kind='alarm',
                        data__entry_id=entry_id,
                        deleted_at__isnull=True)
                .first())
    except Entry.DoesNotExist:
        return None


def events_since(alarm_entry_id: str, hours: int, limit: int = 500) -> list[dict]:
    """Event entries with fire_time within the last N hours.

    Returns dicts with useful denormalised fields for the template.
    """
    event_entry_id = _event_entry_id_for(alarm_entry_id)
    cutoff = time.time() - hours * 3600
    qs = (Entry.objects
          .filter(context_id=CONTEXT_NAME, kind='event',
                  data__entry_id=event_entry_id,
                  archived=False, deleted_at__isnull=True,
                  timestamp_created__gte=cutoff)
          .order_by('-timestamp_created')[:limit])
    return [_event_to_dict(e) for e in qs]


def count_events_since(alarm_entry_id: str, hours: int) -> int:
    event_entry_id = _event_entry_id_for(alarm_entry_id)
    cutoff = time.time() - hours * 3600
    return (Entry.objects
            .filter(context_id=CONTEXT_NAME, kind='event',
                    data__entry_id=event_entry_id,
                    archived=False, deleted_at__isnull=True,
                    timestamp_created__gte=cutoff)
            .count())


def _active_events_qs(alarm_entry_id: str):
    """All non-archived event rows for this alarm, filtered in Python for
    JSON-null clear_time (Django's __isnull on JSON paths only catches
    missing keys, not ``null``-valued ones)."""
    event_entry_id = _event_entry_id_for(alarm_entry_id)
    rows = (Entry.objects
            .filter(context_id=CONTEXT_NAME, kind='event',
                    data__entry_id=event_entry_id,
                    archived=False, deleted_at__isnull=True)
            .order_by('-timestamp_created'))
    return [e for e in rows if (e.data or {}).get('clear_time') is None]


def active_event_count(alarm_entry_id: str) -> int:
    return len(_active_events_qs(alarm_entry_id))


def active_event_rows(alarm_entry_id: str) -> list:
    """Raw Entry objects for currently-active events of this alarm."""
    return _active_events_qs(alarm_entry_id)


def active_events(alarm_entry_id: str) -> list[dict]:
    """Present-state view: one row per currently-active event for this alarm."""
    out: list[dict] = []
    for e in _active_events_qs(alarm_entry_id):
        d = e.data or {}
        ft = d.get('fire_time')
        out.append({
            'id': e.id,
            'subject': d.get('subject') or e.title or '?',
            'dedupe_key': d.get('dedupe_key') or '',
            'fire_time': ft,
            'fire_time_dt': _ts_to_dt(ft),
            'last_seen': d.get('last_seen'),
            'metric': _event_metric(d),
        })
    return out


def _event_metric(data: dict) -> str:
    """Trigger metric as a display string.

    Preferred: detection set an explicit `metric` key (formatted string).
    Fallback: derive from `computed_failurerate` for old rows that
    predate the metric field.
    """
    m = data.get('metric')
    if isinstance(m, str) and m:
        return m
    cfr = data.get('computed_failurerate')
    if isinstance(cfr, (int, float)):
        return f"{cfr*100:.1f}%"
    return ''


def _ts_to_dt(ts):
    if ts is None or ts == '':
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def events_for_task(alarm_entry_id: str, dedupe_key: str,
                    hours: int) -> list[dict]:
    """All events for one (alarm, entity) in the last N hours, reverse chron."""
    event_entry_id = _event_entry_id_for(alarm_entry_id)
    cutoff = time.time() - hours * 3600
    qs = (Entry.objects
          .filter(context_id=CONTEXT_NAME, kind='event',
                  data__entry_id=event_entry_id,
                  data__dedupe_key=dedupe_key,
                  archived=False, deleted_at__isnull=True,
                  timestamp_created__gte=cutoff)
          .order_by('-timestamp_created'))
    return [_event_to_dict(e) for e in qs]


def task_history_bins(alarm_entry_id: str, dedupe_key: str,
                      hours: int) -> list[dict]:
    """One row per engine tick in the last N hours: state of this (alarm,
    entity) at that tick.

    state ∈ {'firing', 'clear', 'unknown'}:
      - 'firing' — at that tick, an event for this task had fire_time
        ≤ tick ≤ (clear_time or now), i.e. the alarm was active.
      - 'clear' — the tick ran cleanly and the task was not firing.
      - 'unknown' — the tick errored or didn't finish (no truth).
    """
    now = time.time()
    cutoff = now - hours * 3600
    event_entry_id = _event_entry_id_for(alarm_entry_id)

    # All engine runs in window, oldest first.
    runs = (Entry.objects
            .filter(context_id=CONTEXT_NAME, kind='engine_run',
                    deleted_at__isnull=True,
                    timestamp_created__gte=cutoff)
            .order_by('timestamp_created'))

    # All events for this (alarm, entity) whose interval intersects the
    # window. An event is a ∞ interval [fire_time, clear_time|now]; it
    # intersects [cutoff, now] unless clear_time < cutoff.
    evs = (Entry.objects
           .filter(context_id=CONTEXT_NAME, kind='event',
                   data__entry_id=event_entry_id,
                   data__dedupe_key=dedupe_key,
                   archived=False, deleted_at__isnull=True))
    intervals: list[tuple[float, float]] = []
    for e in evs:
        d = e.data or {}
        ft = float(d.get('fire_time') or 0)
        ct = d.get('clear_time')
        ct_f = float(ct) if ct is not None else now
        if ct_f < cutoff:
            continue
        intervals.append((ft, ct_f))

    bins: list[dict] = []
    for run in runs:
        rd = run.data or {}
        tick = float(rd.get('started_at') or run.timestamp_created)
        per_alarm = rd.get('per_alarm') or {}
        pa = per_alarm.get(alarm_entry_id) or {}
        errored = bool(pa.get('errors')) or (rd.get('finished_at') is None)
        firing = any(ft <= tick <= ct for ft, ct in intervals)
        if errored:
            state = 'unknown'
        elif firing:
            state = 'firing'
        else:
            state = 'clear'
        bins.append({
            'tick': tick,
            'state': state,
            'run_id': run.id,
        })
    return bins


def last_fired(alarm_entry_id: str):
    """Most recent moment this alarm was observed firing.

    For active events this is the tick's last_seen (bumped every tick).
    For cleared events this is the clear_time. Taking the max across
    all events gives "the last tick at which anything was firing" —
    which for an alarm that's currently active is the most recent cron
    tick.
    """
    event_entry_id = _event_entry_id_for(alarm_entry_id)
    qs = (Entry.objects
          .filter(context_id=CONTEXT_NAME, kind='event',
                  data__entry_id=event_entry_id,
                  archived=False, deleted_at__isnull=True))
    best = 0.0
    for e in qs:
        d = e.data or {}
        for k in ('last_seen', 'clear_time', 'fire_time'):
            v = d.get(k)
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv > best:
                best = fv
        if e.timestamp_created and e.timestamp_created > best:
            best = float(e.timestamp_created)
    return best if best > 0 else None


def get_event(event_uuid: str) -> dict | None:
    try:
        e = Entry.objects.get(id=event_uuid, context_id=CONTEXT_NAME,
                              kind='event', deleted_at__isnull=True)
    except Entry.DoesNotExist:
        return None
    return _event_to_dict(e)


def versions_for(entry_uuid: str, limit: int = 50) -> list[dict]:
    qs = (EntryVersion.objects
          .filter(entry_id=entry_uuid)
          .order_by('-version_num')[:limit])
    return [{
        'id': v.id,
        'version_num': v.version_num,
        'title': v.title,
        'content': v.content,
        'data': v.data,
        'changed_by': v.changed_by,
        'timestamp': v.timestamp,
        'preview': v.title or ((v.content or '').splitlines()[0][:120] if v.content else ''),
        'line_count': (v.content or '').count('\n') + (1 if (v.content or '') else 0),
    } for v in qs]


def recent_runs(limit: int = 20) -> list[dict]:
    qs = (Entry.objects
          .filter(context_id=CONTEXT_NAME, kind='engine_run',
                  archived=False, deleted_at__isnull=True)
          .order_by('-timestamp_created')[:limit])
    out = []
    for e in qs:
        data = dict(e.data or {})
        # Normalise legacy key names so templates read one shape.
        if 'per_alarm' not in data and 'per_check' in data:
            data['per_alarm'] = data['per_check']
        if 'alarms_run' not in data and 'checks_run' in data:
            data['alarms_run'] = data['checks_run']
        out.append({'id': e.id, 'data': data})
    return out


def quiet_alarms(quiet_ticks: int = 3, history_ticks: int = 12) -> set[str]:
    """Alarm entry_ids that look suspiciously silent.

    An alarm is flagged quiet if:
      - All of the last `quiet_ticks` successful engine runs (errors==0
        for that alarm) saw zero detections for it, AND
      - At least one run in the last `history_ticks` DID see detections
        for it.

    Purely heuristic. A broken alarm that returns nothing looks identical
    to a healthy quiet alarm until it has prior non-zero history, so this
    only catches recently-gone-silent cases. Good enough to surface.
    """
    recent = recent_runs(limit=history_ticks)
    if len(recent) < quiet_ticks:
        return set()
    by_alarm_recent: dict[str, list[int]] = {}
    by_alarm_history: dict[str, int] = {}
    for i, r in enumerate(recent):
        per = (r['data'].get('per_alarm') or {})
        for eid, pc in per.items():
            if (pc or {}).get('errors'):
                continue  # errored run doesn't count toward quiet
            seen = int((pc or {}).get('alarms_seen') or 0)
            if i < quiet_ticks:
                by_alarm_recent.setdefault(eid, []).append(seen)
            by_alarm_history[eid] = by_alarm_history.get(eid, 0) + seen
    out: set[str] = set()
    for eid, recent_seens in by_alarm_recent.items():
        if len(recent_seens) < quiet_ticks:
            continue
        if any(recent_seens):
            continue
        if by_alarm_history.get(eid, 0) > 0:
            out.add(eid)
    return out


def engine_health() -> dict:
    """Traffic light for the dashboard header."""
    try:
        last = (Entry.objects
                .filter(context_id=CONTEXT_NAME, kind='engine_run',
                        deleted_at__isnull=True)
                .order_by('-timestamp_created').first())
    except Exception:  # noqa: BLE001
        return {'status': 'unknown', 'reasons': ['DB not reachable.']}

    if last is None:
        return {'status': 'unknown', 'reasons': ['Engine has never run.']}

    data = last.data or {}
    finished = data.get('finished_at')
    reasons: list[str] = []
    status = 'ok'
    if not finished:
        reasons.append('Last engine run did not finish.')
        status = 'warn'
    else:
        age = time.time() - float(finished)
        if age > 15 * 60:
            reasons.append(
                f'Engine stale: last run finished {int(age // 60)} min ago.')
            status = 'bad'
    if data.get('errors'):
        reasons.append(f"Last run had {data['errors']} error(s).")
        status = 'bad'
    if not reasons:
        reasons.append('All checks healthy.')
    return {'status': status, 'reasons': reasons, 'last_run': data,
            'last_run_id': last.id}


# ── internal helpers ───────────────────────────────────────────────────────

# ── teams ────────────────────────────────────────────────────────────────

def list_teams() -> list[dict]:
    """All non-archived teams in the 'teams' context."""
    qs = (Entry.objects
          .filter(context_id=TEAMS_CONTEXT, kind='team',
                  archived=False, deleted_at__isnull=True)
          .order_by('name'))
    out = []
    for e in qs:
        out.append({
            'id': e.id,
            'name': e.name,                       # '@prodops'
            'title': e.title,
            'content': e.content,
            'members': _parse_recipient_tokens(e.content),
            'created': e.timestamp_created,
            'modified': e.timestamp_modified,
        })
    return out


def get_team(at_name: str) -> Entry | None:
    """Fetch a team by its @name. Accepts with-or-without leading '@'."""
    if not at_name:
        return None
    if not at_name.startswith('@'):
        at_name = '@' + at_name
    return (Entry.objects
            .filter(context_id=TEAMS_CONTEXT, kind='team', name=at_name,
                    archived=False, deleted_at__isnull=True)
            .first())


def get_team_by_id(entry_id: str) -> Entry | None:
    try:
        return Entry.objects.get(id=entry_id, context_id=TEAMS_CONTEXT,
                                 kind='team', deleted_at__isnull=True)
    except Entry.DoesNotExist:
        return None


# ── recipient parsing / expansion ────────────────────────────────────────

def _parse_recipient_tokens(raw) -> list[str]:
    """Split a string or list of strings into normalised recipient tokens.

    Tokens may be separated by commas, whitespace, or both. Blank tokens
    dropped. Each token is either an email address or an @<teamname>.
    Returns the list in the order given, deduped (case-insensitive match
    for emails; @names kept as-is).
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        parts: list[str] = []
        for chunk in raw:
            parts.extend(_parse_recipient_tokens(chunk))
        return _dedup_preserve(parts)
    # String path
    s = str(raw)
    # Normalise commas to whitespace for a single split.
    for sep in [',', ';', '\n', '\r', '\t']:
        s = s.replace(sep, ' ')
    tokens = [t.strip() for t in s.split(' ')]
    return _dedup_preserve([t for t in tokens if t])


def _dedup_preserve(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in seq:
        key = t.lower() if '@' in t and not t.startswith('@') else t
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def parse_recipients_input(text) -> list[str]:
    """Public entry point used by views/engine to normalise user input."""
    return _parse_recipient_tokens(text)


def expand_recipients(tokens) -> tuple[list[str], list[str]]:
    """Expand @<team> tokens into their member emails.

    Returns (emails, unresolved). `emails` is the final dedup'd list of
    deliverable addresses. `unresolved` is a list of @<team> tokens that
    didn't resolve — callers should log but not fail on those.
    """
    final: list[str] = []
    unresolved: list[str] = []
    for tok in _parse_recipient_tokens(tokens):
        if tok.startswith('@'):
            team = get_team(tok)
            if team is None or not team.content.strip():
                unresolved.append(tok)
                continue
            final.extend(_parse_recipient_tokens(team.content))
        else:
            final.append(tok)
    return _dedup_preserve(final), unresolved


# ── internal helpers ─────────────────────────────────────────────────────

def _event_entry_id_for(alarm_entry_id: str) -> str:
    if alarm_entry_id.startswith('alarm_'):
        return 'event_' + alarm_entry_id[len('alarm_'):]
    return 'event_' + alarm_entry_id


_EVENT_INTERNAL_KEYS = {
    'entry_id', 'fire_time', 'clear_time', 'last_seen', 'last_notified',
    'dedupe_key', 'subject', 'recipients', 'alarm_config_id', 'severity',
}


def _event_to_dict(e: Entry) -> dict:
    data = e.data or {}
    fire_time = data.get('fire_time')
    clear_time = data.get('clear_time')
    # Context data shown on the event-detail page: strip plumbing keys
    # (these already have dedicated rows at the top of the page).
    context_data = {k: v for k, v in data.items()
                    if k not in _EVENT_INTERNAL_KEYS}
    return {
        'id': e.id,
        'title': e.title,
        'entry_id': data.get('entry_id'),
        'subject': data.get('subject', ''),
        'dedupe_key': data.get('dedupe_key'),
        'fire_time': fire_time,
        'clear_time': clear_time,
        'last_seen': data.get('last_seen'),
        'state': 'active' if clear_time is None else 'cleared',
        'recipients': data.get('recipients') or [],
        'content': e.content,
        'data': data,
        'context_data': context_data,
        'timestamp_created': e.timestamp_created,
        'timestamp_modified': e.timestamp_modified,
    }
