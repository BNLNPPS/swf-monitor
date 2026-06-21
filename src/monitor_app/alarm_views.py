"""Alarm dashboard + editor views.

The dashboard lives at /swf-monitor/alarms/. It renders:
  1. A top summary table (one row per alarm config) — last-N-hours counts,
     last-fired time.
  2. A per-alarm section for each active config — config metadata + body +
     in-window events + [Edit] link.
  3. A recent-engine-runs table (for engine health visibility).

Editor: /swf-monitor/alarms/<alarm_entry_id>/edit/ — CodeMirror on the body
(content), form fields for params/recipients/enabled. Autosave
every 10s via POST; version history rendered inline, click to restore.

Autosave endpoint: POST /swf-monitor/alarms/<alarm_entry_id>/autosave/ with JSON
body. Returns {version_num, modified}.

The pre_save signal on Entry (signals.py) owns version snapshotting; these
views never write EntryVersion directly.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
import traceback
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

_EASTERN = ZoneInfo('America/New_York')


def _to_dt(value):
    """Float Unix ts → aware datetime, else pass through."""
    if value is None or value == '':
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=_EASTERN)
        except (OSError, OverflowError, ValueError):
            return None
    return value


def _recipients_to_text(value):
    """Render stored recipients for the textarea verbatim.

    Storage may be str (new — user-typed, preserved) or list[str] (legacy —
    pre-change rows). Strings pass through untouched. Lists are rejoined
    with ', ' so they render as one line in the editor.
    """
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ', '.join(str(v) for v in value)
    return str(value)

from . import alarms_data
from .models import Entry, EntryContext, EntryVersion
from .signals import set_changed_by


# ── alarm-module loader (for the editor's help panel + test endpoint) ─────
#
# The engine code lives in <repo>/alarms/swf_alarms/. This file lives in
# <repo>/src/monitor_app/alarm_views.py. Two directories up from this file
# is the repo root, so the engine package sits at "../../alarms/swf_alarms".

_ALARMS_PKG_PARENT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'alarms'))


def _ensure_engine_importable() -> None:
    if _ALARMS_PKG_PARENT not in sys.path and os.path.isdir(_ALARMS_PKG_PARENT):
        sys.path.insert(0, _ALARMS_PKG_PARENT)


def _alarm_module(entry_id: str):
    _ensure_engine_importable()
    name = entry_id[len('alarm_'):] if entry_id.startswith('alarm_') else entry_id
    return importlib.import_module(f"swf_alarms.alarms.{name}")


def _detection_detail_from_event(content: str) -> str:
    """Peel the description+separator off an event's stored content to
    leave just the detection detail (what `detect()` put in
    `body_context`). Events are persisted with the full single-detection
    body, i.e. ``<alarm.content>\\n\\n---\\n\\n<body_context>``."""
    if not content:
        return ''
    marker = '\n\n---\n\n'
    if marker in content:
        return content.split(marker, 1)[1]
    return content


def _preview_current_state(entry_id: str, description: str) -> tuple[str, str]:
    """Build the (subject, body) email preview for this alarm's CURRENT
    state — i.e. what would go out right now if we emailed every
    currently-active event as one bundle.

    Matches the engine's `_compose_bundle` layout: description + ``---`` +
    a single list of active entities, each with its body_context indented.
    Returns ('', '') when nothing is active.
    """
    active = alarms_data.active_event_rows(entry_id)
    n = len(active)
    if n == 0:
        return ('', '')
    subject = f"[{entry_id}] {n} detection(s) currently active"
    parts: list[str] = []
    desc = (description or '').rstrip()
    if desc:
        parts.append(desc)
        parts.append('')
        parts.append('---')
        parts.append('')
    parts.append(f"CURRENTLY ACTIVE ({n}):")
    parts.append('')
    for i, ev in enumerate(active, 1):
        data = ev.data or {}
        subj = data.get('subject') or ev.title or '?'
        detail = _detection_detail_from_event(ev.content or '')
        parts.append(f"  [{i}] {subj}")
        for line in detail.splitlines():
            parts.append(f"      {line}")
        parts.append('')
    body = '\n'.join(parts).rstrip() + '\n'
    return (subject, body)


def _alarm_params_meta(entry_id: str) -> list[dict]:
    """Read the alarm module's PARAMS for the editor help panel.

    Returns a list of {name, type, required, default, description} rows
    in declaration order. Returns [] on any import failure — the editor
    just hides the help panel.
    """
    try:
        mod = _alarm_module(entry_id)
    except Exception:
        return []
    params = getattr(mod, 'PARAMS', None) or {}
    rows: list[dict] = []
    for k, v in params.items():
        if not isinstance(v, dict):
            continue
        t = v.get('type')
        rows.append({
            'name': k,
            'type': getattr(t, '__name__', str(t)) if t else '',
            'required': bool(v.get('required')),
            'default': v.get('default'),
            'has_default': 'default' in v,
            'description': v.get('description') or '',
        })
    return rows


# ── dashboard ─────────────────────────────────────────────────────────────

def alarms_dashboard(request):
    try:
        hours = max(1, int(request.GET.get('hours', 24)))
    except (TypeError, ValueError):
        hours = 24

    configs = alarms_data.alarm_configs()
    quiet = alarms_data.quiet_alarms()

    def _params_table(entry_id: str, params: dict) -> list[dict]:
        """Per-alarm param rows for the main-page table.

        One row per param declared in the alarm module's PARAMS, plus
        any extra keys stored on the config that the module doesn't
        declare (those get a warning description).
        """
        meta_list = _alarm_params_meta(entry_id)
        rows: list[dict] = []
        seen: set[str] = set()
        for m in meta_list:
            k = m['name']
            seen.add(k)
            rows.append({
                'name': k,
                'value': params.get(k),
                'has_value': k in params,
                'type': m.get('type') or '',
                'required': bool(m.get('required')),
                'default': m.get('default'),
                'has_default': bool(m.get('has_default')),
                'description': m.get('description') or '',
            })
        for k, v in params.items():
            if k in seen:
                continue
            rows.append({
                'name': k,
                'value': v,
                'has_value': True,
                'type': '',
                'required': False,
                'default': None,
                'has_default': False,
                'description': '(not declared in module PARAMS)',
            })
        return rows

    # Per-config: count + last-fired + events-since (reversed chron).
    sections = []
    summary_rows = []
    for cfg in configs:
        eid = cfg['entry_id']
        count = alarms_data.count_events_since(eid, hours)
        last = alarms_data.last_fired(eid)
        active = alarms_data.active_event_count(eid)
        active_rows = alarms_data.active_events(eid)
        is_quiet = eid in quiet
        summary_rows.append({
            'entry_id': eid,
            'name': cfg['name'],
            'title': cfg.get('title', ''),
            'enabled': cfg['enabled'],
            'count': count,
            'active': active,
            'last_fired': last,
            'last_fired_dt': _to_dt(last),
            'quiet': is_quiet,
        })
        preview_subject, preview_body = _preview_current_state(
            eid, cfg.get('content') or '')
        sections.append({
            'config': cfg,
            'count': count,
            'active': active,
            'last_fired': last,
            'active_rows': active_rows,
            'quiet': is_quiet,
            'params_table': _params_table(eid, cfg['params']),
            'preview_subject': preview_subject,
            'preview_body': preview_body,
        })

    # Cron fires */5 — seconds remaining to next 5-min boundary.
    cycle_seconds = 300
    now = time.time()
    next_check_seconds = int(cycle_seconds - (now % cycle_seconds))
    built_at_dt = datetime.fromtimestamp(now, tz=_EASTERN)

    return render(request, 'monitor_app/alarms.html', {
        'hours': hours,
        'summary_rows': summary_rows,
        'sections': sections,
        'teams': alarms_data.list_teams(),
        'health': alarms_data.engine_health(),
        'recent_runs': alarms_data.recent_runs(limit=20),
        'cycle_seconds': cycle_seconds,
        'next_check_seconds': next_check_seconds,
        'built_at_dt': built_at_dt,
        'auto_refresh_seconds': 10,
    })


# ── event detail ──────────────────────────────────────────────────────────

def alarm_run_report(request, run_uuid: str, entry_id: str):
    """Show the per-alarm bundle for one engine run.

    This is exactly what WOULD have been emailed for this alarm on this
    tick (or was, if emails were on and the bundle was non-empty).
    Covers every detection the bundle would have carried — new +
    continuing. No emails are involved in rendering this page.
    """
    try:
        run = Entry.objects.get(id=run_uuid, context_id='swf-alarms',
                                kind='engine_run',
                                deleted_at__isnull=True)
    except Entry.DoesNotExist:
        return HttpResponse(f'Engine run {run_uuid} not found', status=404,
                            content_type='text/plain')
    per_alarm = (run.data or {}).get('per_alarm') or {}
    info = per_alarm.get(entry_id)
    if info is None:
        return HttpResponse(
            f'Alarm {entry_id} did not run in this tick.',
            status=404, content_type='text/plain')

    new_ids = info.get('bundle_new_event_ids') or []
    ren_ids = info.get('bundle_renotify_event_ids') or []
    ev_by_id = {
        e.id: e for e in Entry.objects.filter(
            id__in=list(new_ids) + list(ren_ids),
            context_id='swf-alarms', kind='event')
    }
    # Preserve ordering from the stored lists.
    new_events = [ev_by_id[u] for u in new_ids if u in ev_by_id]
    ren_events = [ev_by_id[u] for u in ren_ids if u in ev_by_id]

    # Pull the alarm config so we can show the description that would
    # sit at the top of the email body.
    alarm = _require_alarm(entry_id)
    description = (alarm.content if alarm else '') or ''

    return render(request, 'monitor_app/alarm_run_report.html', {
        'run': run,
        'run_started_at': (run.data or {}).get('started_at'),
        'run_finished_at': (run.data or {}).get('finished_at'),
        'alarm_entry_id': entry_id,
        'subject': info.get('bundle_subject', ''),
        'description': description,
        'new_events': new_events,
        'continuing_events': ren_events,
        'recipients': info.get('recipients') or [],
        'email_enabled': bool(info.get('enabled')),
        'bundle_sent': bool(info.get('bundle_sent')),
        'errors': info.get('errors'),
        'error_message': info.get('error_message') or '',
    })


def alarm_task_history(request, entry_id: str):
    """Per-entity history for one alarm. One row in a strip of colored
    cells, one cell per engine tick in the last N hours.

    URL: /swf-monitor/alarms/<entry_id>/task/?key=<dedupe_key>&hours=<N>
    """
    dedupe_key = request.GET.get('key', '')
    try:
        hours = max(1, int(request.GET.get('hours', 24)))
    except (TypeError, ValueError):
        hours = 24

    alarm = _require_alarm(entry_id)
    if alarm is None:
        return HttpResponse(f'Alarm config {entry_id} not found',
                            status=404, content_type='text/plain')
    if not dedupe_key:
        return HttpResponse('key query param required',
                            status=400, content_type='text/plain')

    bins = alarms_data.task_history_bins(entry_id, dedupe_key, hours)
    events = alarms_data.events_for_task(entry_id, dedupe_key, hours)

    # Collapse the dedupe_key into a human label: "task:35981" → "task 35981".
    label = dedupe_key.replace(':', ' ', 1)

    # Group the strip into one-hour blocks, each labeled. A label line
    # at the start of a new date is marked so the template can surface
    # the date above the time.
    bin_groups = []
    current = None
    last_date = None
    for b in bins:
        dt = datetime.fromtimestamp(float(b['tick']), tz=_EASTERN)
        hour_key = dt.strftime('%Y-%m-%d %H')
        date_str = dt.strftime('%Y-%m-%d')
        if current is None or current['hour_key'] != hour_key:
            current = {
                'hour_key': hour_key,
                'label': dt.strftime('%H:00'),
                'date': date_str,
                'date_change': last_date != date_str,
                'bins': [],
            }
            last_date = date_str
            bin_groups.append(current)
        current['bins'].append(b)

    return render(request, 'monitor_app/alarm_task_history.html', {
        'alarm_entry_id': entry_id,
        'alarm_title': alarm.title or entry_id,
        'dedupe_key': dedupe_key,
        'label': label,
        'hours': hours,
        'bins': bins,
        'bin_groups': bin_groups,
        'events': events,
    })


def alarm_event_detail(request, event_uuid: str):
    event = alarms_data.get_event(event_uuid)
    if event is None:
        return HttpResponse('Event not found', status=404,
                            content_type='text/plain')
    return render(request, 'monitor_app/alarm_event_detail.html', {
        'event': event,
    })


# ── alarm config editor ───────────────────────────────────────────────────

def _require_alarm(entry_id: str) -> Entry | None:
    return alarms_data.get_alarm_config_by_entry_id(entry_id)


@login_required
def alarm_config_edit(request, entry_id: str):
    """GET: render CodeMirror editor for an alarm config."""
    alarm = _require_alarm(entry_id)
    if alarm is None:
        return HttpResponse(f'Alarm config {entry_id} not found',
                            status=404, content_type='text/plain')

    versions = alarms_data.versions_for(alarm.id, limit=50)

    data = alarm.data or {}
    # First-class form fields — separate from the JSON editor so ops
    # can tweak routing/enabled without JSON-syntax risk.
    return render(request, 'monitor_app/alarm_config_edit.html', {
        'alarm': alarm,
        'alarm_entry_id': entry_id,
        'title': alarm.title or '',
        'content': alarm.content or '',
        'line_count': (alarm.content or '').count('\n') + 1,
        'enabled': bool(data.get('enabled', True)),
        'recipients_text': _recipients_to_text(data.get('recipients')),
        'renotification_window_hours': data.get('renotification_window_hours') or 0,
        # Only the params the alarm code reads — kind/internal dispatch
        # is not user-facing.
        'data_json': json.dumps(
            dict(data.get('params') or {}), indent=2, sort_keys=True),
        'params_meta': _alarm_params_meta(entry_id),
        'versions_json': json.dumps(versions, default=str),
    })


@login_required
@csrf_exempt  # editor posts JSON; token mismatch handled by auth
@require_POST
def alarm_config_save(request, entry_id: str):
    """POST: save edits to an alarm config. JSON body:
        {
          "content": "...",
          "data": { enabled, recipients, params },
          "autosave": true|false   (optional; marks changed_by=autosave)
        }
    Returns {version_num, modified}.
    """
    alarm = _require_alarm(entry_id)
    if alarm is None:
        return JsonResponse({'error': 'not found'}, status=404)

    try:
        payload = json.loads(request.body or b'{}')
    except json.JSONDecodeError as e:
        return JsonResponse({'error': f'bad json: {e}'}, status=400)

    new_content = payload.get('content')
    new_title = payload.get('title')
    new_partial = payload.get('data') or {}
    if new_content is None:
        new_content = alarm.content
    if new_title is None:
        new_title = alarm.title

    # Merge partial edits onto existing data, preserving entry_id and the
    # internal `kind` dispatch key (not user-editable).
    existing_data = dict(alarm.data or {})
    # The editor's JSON block is the params dict, nothing else.
    if new_partial:
        existing_data['params'] = new_partial
    # First-class fields come at the top level of the payload.
    if 'enabled' in payload:
        existing_data['enabled'] = bool(payload['enabled'])
    if 'recipients' in payload:
        # Store verbatim — do NOT re-format user input. The engine splits
        # on commas/whitespace at send-time.
        existing_data['recipients'] = payload['recipients']
    if 'renotification_window_hours' in payload:
        try:
            existing_data['renotification_window_hours'] = int(float(
                payload['renotification_window_hours']))
        except (TypeError, ValueError):
            pass
    if 'entry_id' not in existing_data:
        existing_data['entry_id'] = entry_id  # keep slug stable

    changed_by = 'autosave' if payload.get('autosave') else 'web_ui'
    if request.user.is_authenticated:
        changed_by = f"{changed_by}:{request.user.username}"
    set_changed_by(changed_by)

    alarm.title = new_title
    alarm.content = new_content
    alarm.data = existing_data
    alarm.timestamp_modified = time.time()
    alarm.save()

    # Report the new latest version number (signal may or may not have
    # snapshotted depending on whether the change was substantive).
    latest = (EntryVersion.objects.filter(entry=alarm)
              .order_by('-version_num').values('version_num').first())
    return JsonResponse({
        'version_num': latest['version_num'] if latest else 0,
        'modified': alarm.timestamp_modified,
    })


@login_required
@csrf_exempt
@require_POST
def alarm_test(request, entry_id: str):
    """POST: run the alarm's detect() once against live data; never emails.

    JSON body: {"params": {...}}  — overrides the stored params.
    Response: {"detections": [...], "count": int, "error": str?, "elapsed_ms": int}

    Uses the engine's own Client (http-only), so Django's venv must have
    httpx installed. Engine code is made importable ad-hoc via sys.path.
    """
    alarm = _require_alarm(entry_id)
    if alarm is None:
        return JsonResponse({'error': 'not found'}, status=404)
    try:
        payload = json.loads(request.body or b'{}')
    except json.JSONDecodeError as e:
        return JsonResponse({'error': f'bad json: {e}'}, status=400)

    params = payload.get('params')
    if params is None:
        params = dict((alarm.data or {}).get('params') or {})

    try:
        mod = _alarm_module(entry_id)
    except Exception as e:
        return JsonResponse({'error': f'cannot import alarm module: {e}'},
                            status=500)

    # Engine client pulls from swf-monitor's own API.
    try:
        from swf_alarms.fetch import Client, FetchError  # type: ignore
    except Exception as e:
        return JsonResponse({'error': f'cannot import engine client: {e}'},
                            status=500)

    base_url = getattr(
        settings,
        'SWF_ALARMS_BASE_URL',
        getattr(settings, 'SWF_MONITOR_URL',
                'https://pandaserver02.sdcc.bnl.gov/swf-monitor'),
    )
    client = Client(base_url, timeout=30.0)

    t0 = time.time()
    detections: list[dict] = []
    error: str | None = None
    try:
        for det in mod.detect(client, params):
            detections.append({
                'dedupe_key': det.dedupe_key,
                'subject': det.subject,
                'body_context': det.body_context,
                'extra_data': det.extra_data,
            })
            if len(detections) >= 200:  # cap for sanity
                break
    except FetchError as e:
        error = f'fetch error: {e}'
    except Exception:  # noqa: BLE001
        error = traceback.format_exc()
    elapsed_ms = int((time.time() - t0) * 1000)

    return JsonResponse({
        'count': len(detections),
        'detections': detections,
        'error': error,
        'elapsed_ms': elapsed_ms,
    })




@login_required
def alarm_config_version(request, entry_id: str, version_num: int):
    """GET: return a specific version's content+data as JSON (for restore)."""
    alarm = _require_alarm(entry_id)
    if alarm is None:
        return JsonResponse({'error': 'not found'}, status=404)
    try:
        v = EntryVersion.objects.get(entry_id=alarm.id, version_num=version_num)
    except EntryVersion.DoesNotExist:
        return JsonResponse({'error': 'version not found'}, status=404)
    return JsonResponse({
        'version_num': v.version_num,
        'title': v.title,
        'content': v.content,
        'data': v.data,
        'changed_by': v.changed_by,
        'timestamp': v.timestamp,
    })


# ── teams ─────────────────────────────────────────────────────────────────

def _team_at_name(raw: str) -> str:
    """Normalise to '@<name>'. Strips leading @s and whitespace."""
    raw = (raw or '').strip()
    raw = raw.lstrip('@')
    return '@' + raw if raw else ''


@login_required
def team_new(request):
    """GET /alarms/teams/new/ — render the team editor in create-mode
    with an empty name input. Save in this mode POSTs to team_create,
    which returns the new team's edit URL; the client navigates there.

    POST to the same URL is forwarded to team_create so the single URL
    `alarms/teams/new/` serves the whole create cycle.
    """
    if request.method == 'POST':
        return team_create(request)
    return render(request, 'monitor_app/team_edit.html', {
        'team': None,
        'team_name': '',
        'title': '',
        'content': '',
        'versions_json': '[]',
        'create_mode': True,
    })


@login_required
@csrf_exempt
@require_POST
def team_create(request):
    """Create a new team entry. POST body JSON: {name, title, content}.

    Redirects to the editor page (well — returns JSON with edit URL;
    client navigates).
    """
    try:
        payload = json.loads(request.body or b'{}')
    except json.JSONDecodeError as e:
        return JsonResponse({'error': f'bad json: {e}'}, status=400)

    at_name = _team_at_name(payload.get('name') or '')
    if not at_name or at_name == '@':
        return JsonResponse({'error': 'name required'}, status=400)

    try:
        ctx = EntryContext.objects.get(name='teams')
    except EntryContext.DoesNotExist:
        return JsonResponse({'error': 'teams context missing — run migrations'},
                            status=500)

    if Entry.objects.filter(context=ctx, kind='team', name=at_name).exists():
        return JsonResponse({'error': f'team {at_name} already exists'},
                            status=409)

    changed_by = 'web_ui'
    if request.user.is_authenticated:
        changed_by += f':{request.user.username}'
    set_changed_by(changed_by)

    e = Entry.objects.create(
        id=str(uuid.uuid4()),
        title=payload.get('title') or at_name[1:].capitalize(),
        content=(payload.get('content') or '').strip(),
        kind='team',
        context=ctx,
        name=at_name,
        data={'entry_id': f'team_{at_name[1:]}'},
        status='active',
        archived=False,
        timestamp_created=time.time(),
        timestamp_modified=time.time(),
    )
    return JsonResponse({
        'id': e.id,
        'name': e.name,
        'edit_url': f'/alarms/teams/{at_name}/edit/',
    })


def _require_team(at_name: str) -> Entry | None:
    return alarms_data.get_team(at_name)


@login_required
def team_edit(request, at_name: str):
    team = _require_team(at_name)
    if team is None:
        return HttpResponse(f'Team {at_name} not found', status=404,
                            content_type='text/plain')
    versions = alarms_data.versions_for(team.id, limit=50)
    return render(request, 'monitor_app/team_edit.html', {
        'team': team,
        'team_name': team.name,
        'title': team.title or '',
        'content': team.content or '',
        'versions_json': json.dumps(versions, default=str),
    })


@login_required
@csrf_exempt
@require_POST
def team_save(request, at_name: str):
    team = _require_team(at_name)
    if team is None:
        return JsonResponse({'error': 'not found'}, status=404)
    try:
        payload = json.loads(request.body or b'{}')
    except json.JSONDecodeError as e:
        return JsonResponse({'error': f'bad json: {e}'}, status=400)

    changed_by = 'autosave' if payload.get('autosave') else 'web_ui'
    if request.user.is_authenticated:
        changed_by = f'{changed_by}:{request.user.username}'
    set_changed_by(changed_by)

    if 'title' in payload:
        team.title = payload['title'] or ''
    if 'content' in payload:
        team.content = payload['content'] or ''
    team.timestamp_modified = time.time()
    team.save()

    latest = (EntryVersion.objects.filter(entry=team)
              .order_by('-version_num').values('version_num').first())
    return JsonResponse({
        'version_num': latest['version_num'] if latest else 0,
        'modified': team.timestamp_modified,
    })


@login_required
def team_version(request, at_name: str, version_num: int):
    team = _require_team(at_name)
    if team is None:
        return JsonResponse({'error': 'not found'}, status=404)
    try:
        v = EntryVersion.objects.get(entry_id=team.id, version_num=version_num)
    except EntryVersion.DoesNotExist:
        return JsonResponse({'error': 'version not found'}, status=404)
    return JsonResponse({
        'version_num': v.version_num,
        'title': v.title,
        'content': v.content,
        'data': v.data,
        'changed_by': v.changed_by,
        'timestamp': v.timestamp,
    })
