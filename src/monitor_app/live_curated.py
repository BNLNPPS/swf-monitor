"""The curated epicprod-live page (ACTION_STREAM.md, Curated view).

The dashboard's epicprod-live title opens the live stream's last day as a
reader wants it: one line per event in six groups, newest first in each,
with who and why. Machinery and the step records behind an event another
line reports are left out and counted, never dropped silently (Torre
Wenaus, 2026-10-28, asking for considered curation instead of the raw
list). An action no rule names is shown under Findings and decisions
until it is given its place here.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from monitor_app.live_notices import (FAILURE_OUTCOMES, MAINTENANCE, OPERATION_LABELS,
                                      ROUTINE_SUCCESSES, _node_guard_name, board_text,
                                      failure_key, node_guard_text, site_node_guard_fields)
from monitor_app.short_names import short_name

logger = logging.getLogger(__name__)

WINDOW_H = 24
MAX_WINDOW_H = 168
GROUPS = (
    ('problems', 'Problems',
     'Warnings and errors, one line per subject: the newest, how often it recurred, '
     'and whether it has recovered.'),
    ('findings', 'Findings and decisions',
     'Findings, settings, limits and other decisions, with who and why.'),
    ('tasks', 'Task operations',
     "Submissions, pauses, resumes, finishes and moves, with who and why; a task's "
     'moves share one line.'),
    ('feeds', 'Feeds', "Work released to a site by the front or the site's own feeder."),
    ('node_guard', 'Node guard', 'Nodes newly excluded.'),
    ('assessments', 'Assessments', 'Assessments registered, with their verdicts.'),
)
PROBLEM_LEVELS = frozenset({'WARNING', 'ERROR', 'CRITICAL'})
# Machinery: routine passes, bookkeeping and the EVGEN intake's own steps.
MACHINERY = MAINTENANCE | ROUTINE_SUCCESSES | frozenset({
    'gke_pilot_cycle', 'live_watch', 'es_closeout', 'front_cycle', 'rucio_arrivals',
    'segfault_finding_set', 'segfault_diagnosis', 'segfault_diagnosis_triggered',
    'evgen_register', 'evgen_register_request', 'evgen_events',
    'dataset_expected_events_set', 'registered_sample_intake', 'task_evgen_input',
})
# Steps behind an event another line reports: the front's idle move and the
# task guard's pause request are the Move and Pause lines' why.
STEPS = frozenset({'idle_move', 'task_failure_guard', 'live_cleared'})
# Informational states Production now already carries: OSG's fair-share
# hold is its normal mode of operation (Torre Wenaus, 2026-10-28).
INFORMATIONAL = frozenset({'osg_throttle'})
TASK_ACTIONS = frozenset({'panda_task_operation', 'evgen_task_submit'})
# The feeders, the outcome that releases work, and the feeder's name.
FEEDERS = {'front_decision': ('fed', 'Front'),
           'google_production': ('submitted', 'Google feeder'),
           'perlmutter_production': ('submitted', 'Perlmutter feeder')}
AUTOMATION = {'front_cron': 'front', 'node_guard_cron': 'node guard',
              'osgsub01-reporter': 'OSG submit reporter'}
ASSESSMENT_KINDS = {'daily': 'Daily', 'weekly': 'Weekly', 'live_watch': 'Live watch'}


def group_of(levelname, extra):
    """The group a live record is shown in, or None when it is left out."""
    action = str(extra.get('action') or '')
    outcome = str(extra.get('outcome') or '')
    if action in INFORMATIONAL:
        return None
    if action == 'assessment_register':
        return 'assessments'
    if action == 'production_finding':
        return 'findings'
    if action == 'node_guard_decision':
        # New exclusions only: a re-trip from half open is a node already
        # on the blacklist, a shadow suggestion excludes nothing.
        new = outcome == 'excluded' and extra.get('old_state') in (None, '', 'clear')
        return 'node_guard' if new else None
    if action in TASK_ACTIONS:
        return 'tasks'
    if action in FEEDERS and outcome == FEEDERS[action][0]:
        return 'feeds'
    if action in STEPS and outcome not in FAILURE_OUTCOMES:
        return None
    if levelname in PROBLEM_LEVELS:
        return 'problems'
    if action in MACHINERY or action in STEPS or action in FEEDERS:
        return None
    return 'findings'


def curated_live(hours=WINDOW_H):
    """The page's record: the window, the groups with their lines, and the
    counts of live records read and left out."""
    from monitor_app.comp_snapshot import _pages
    from monitor_app.epicprod_logging import live_stream_q
    from monitor_app.models import AppLog

    hours = max(1, min(int(hours), MAX_WINDOW_H))
    since = timezone.now() - timedelta(hours=hours)
    rows = list(_pages(AppLog.objects.filter(live_stream_q(), timestamp__gte=since)
                       .order_by('-timestamp')
                       .values('id', 'timestamp', 'levelname', 'message',
                               'instance_name', 'extra_data')))
    sorted_rows = {key: [] for key, _, _ in GROUPS}
    left_out = 0
    for r in rows:
        r['extra_data'] = r['extra_data'] or {}
        group = group_of(r['levelname'], r['extra_data'])
        if group is None:
            left_out += 1
        else:
            sorted_rows[group].append(r)
    people = _people(rows)
    builders = {'problems': _problem_lines, 'findings': _plain_lines,
                'tasks': _task_lines, 'feeds': _feed_lines,
                'node_guard': _node_guard_lines, 'assessments': _assessment_lines}
    groups = []
    for key, title, description in GROUPS:
        try:
            lines = builders[key](sorted_rows[key], rows, people)
        except Exception:
            # One group's failure must not take the page down: its records
            # are shown plainly and the failure is logged.
            logger.exception('curated live page: group %s failed', key)
            lines = _plain_lines(sorted_rows[key], rows, people)
        groups.append({'key': key, 'title': title, 'description': description,
                       'records': len(sorted_rows[key]), 'lines': lines})
    return {'hours': hours, 'since': since, 'records': len(rows),
            'left_out': left_out, 'groups': groups}


def _people(rows):
    """{username: full name} for the people named in the window."""
    from django.contrib.auth import get_user_model
    names = {str(r['extra_data'].get('username') or '') for r in rows} - {''}
    return {u.username: u.get_full_name() for u in
            get_user_model().objects.filter(username__in=names) if u.get_full_name()}


def _who(username, people):
    u = str(username or '')
    if u in AUTOMATION:
        return AUTOMATION[u]
    if u == 'cron' or u.endswith('_cron'):
        return 'cron'
    return people.get(u) or u


def _line(r, people, title=None, detail=None, level=None, who=None, note=''):
    t, d = board_text(r['message'], r['extra_data'])
    return {'id': r['id'], 'at': r['timestamp'], 'level': level or r['levelname'],
            'title': _plain(title or t), 'detail': _plain(detail or d), 'note': _plain(note),
            'who': who if who is not None else _who(r['extra_data'].get('username'), people)}


def _plain_lines(group_rows, rows, people):
    return [_line(r, people) for r in group_rows]


def _problem_lines(group_rows, rows, people):
    """One line per subject, the newest record, as the dashboard's panel
    keeps them (comp_snapshot._subject), noting the repeats and whether the
    failure has recovered, ended or been answered."""
    from monitor_app.comp_snapshot import _last_ok, _subject, _superseded
    from monitor_app.models import AppLog
    cleared = {}
    for c in (AppLog.objects.filter(app_name='epicprod', extra_data__action='live_cleared',
                                    timestamp__gte=group_rows[-1]['timestamp'])
              .values_list('extra_data', flat=True) if group_rows else ()):
        for i in (c or {}).get('cleared_ids') or ():
            cleared[int(i)] = str((c or {}).get('reason') or '')
    subjects = {}
    for r in group_rows:
        subjects.setdefault(_subject(r['instance_name'], r['extra_data']), []).append(r)
    lines = []
    for same in subjects.values():
        r, extra = same[0], same[0]['extra_data']
        notes = []
        if len(same) > 1:
            notes.append(f"{len(same)} times since {_at(same[-1]['timestamp'])}")
        level = None
        outcome = str(extra.get('outcome') or '')
        if r['id'] in cleared:
            notes.append(f"answered: {cleared[r['id']]}")
            level = 'CLEARED'
        elif outcome in FAILURE_OUTCOMES:
            ok = _last_ok(r['instance_name'], extra, failure_key(r['instance_name'], extra))
            if ok and ok > r['timestamp']:
                notes.append(f'recovered {_at(ok)}')
                level = 'CLEARED'
        elif _superseded(r['instance_name'], extra, r['timestamp']):
            notes.append('ended')
            level = 'CLEARED'
        lines.append(_line(r, people, level=level, note='; '.join(notes)))
    return lines


def _task_lines(group_rows, rows, people):
    """A line per task operation with who and why from its operation record
    (PandaTaskOperation), the pending and final records of one operation
    shown once at their final outcome; a task's moves on one line."""
    from monitor_app.models import PandaTaskOperation
    task_ids = set()
    for r in group_rows:
        x = r['extra_data']
        task_ids.update(str(t) for t in x.get('jedi_task_ids') or ())
        if x.get('subject_type') == 'panda_task' and x.get('subject_key'):
            task_ids.add(str(x['subject_key']))
    ops = {}
    since = (group_rows[-1]['timestamp'] - timedelta(hours=1)) if group_rows else None
    for op in (PandaTaskOperation.objects.filter(
            jedi_task_id__in=[int(t) for t in task_ids if t.isdigit()],
            requested_at__gte=since).order_by('requested_at') if task_ids else ()):
        ops.setdefault((str(op.jedi_task_id), op.operation), []).append(op)
    idle = {str(r['extra_data'].get('operation_id')): r['extra_data']
            for r in rows if r['extra_data'].get('action') == 'idle_move'}

    def operation_of(task, operation, at):
        found = None
        for op in ops.get((task, operation), ()):
            if op.requested_at <= at:
                found = op
        return found

    def who_why(op, r):
        if op is None:
            return _who(r['extra_data'].get('username'), people), ''
        evidence = op.evidence or {}
        source = str(op.source or '')
        if source.startswith('front_task_failure_guard'):
            from monitor_app.comp_snapshot import _guard_reason
            return 'front task guard', _guard_reason(evidence) or ''
        if source == 'front_idle_move':
            move = idle.get(str(op.id)) or {}
            return 'front', f"{short_name(move.get('to') or evidence.get('site'))} idle"
        return (_who(op.requested_by, people),
                str(evidence.get('reason') or evidence.get('note') or ''))

    shown, moves, lines = set(), {}, []
    for r in group_rows:
        x = r['extra_data']
        if x.get('action') == 'evgen_task_submit':
            task = x.get('jedi_task_id')
            lines.append(_line(r, people, title=f"Submit · task {task}" if task else 'Submit',
                               detail=str(x.get('subject_key') or r['message'])))
            continue
        operation = str(x.get('operation') or '')
        tasks = [str(t) for t in x.get('jedi_task_ids') or ()] or [str(x.get('subject_key'))]
        op = operation_of(tasks[0], operation, r['timestamp'])
        key = op.id if op is not None else r['id']
        if key in shown:
            continue  # the pending record of an operation shown at its final outcome
        shown.add(key)
        who, why = who_why(op, r)
        if operation == 'reassign' and len(tasks) == 1:
            moves.setdefault(tasks[0], []).append((r, who))
            continue
        label = OPERATION_LABELS.get(operation, operation.capitalize() or 'Operation')
        title = (f"{label} · tasks {', '.join(tasks)}" if len(tasks) > 1
                 else f'{label} · task {tasks[0]}')
        name = x.get('task_name') or ''
        status = '' if x.get('outcome') == 'ok' else str(x.get('reason') or x.get('outcome') or '')
        detail = '; '.join(p for p in (why, status) if p)
        lines.append(_line(r, people, title=title, who=who,
                           detail=f'{name}: {detail}' if name and detail else (name or detail
                                                                               or r['message'])))
    for task, made in moves.items():
        r = made[0][0]
        steps = []
        for m, who in reversed(made):
            mx = m['extra_data']
            step = (f"{short_name(mx.get('previous_site')) or '?'} to "
                    f"{short_name(mx.get('site')) or '?'} {_at(m['timestamp'])}, {who}")
            if mx.get('outcome') != 'ok':
                step += f" ({mx.get('reason') or mx.get('outcome')})"
            steps.append(step)
        worst = max((m['levelname'] for m, _ in made), key=_level_rank)
        title = f'Move ×{len(made)} · task {task}' if len(made) > 1 else f'Move · task {task}'
        name = r['extra_data'].get('task_name') or ''
        lines.append(_line(r, people, title=title, level=worst,
                           who=', '.join(dict.fromkeys(w for _, w in made)),
                           detail=(f'{name}: ' if name else '') + '; '.join(steps)))
    lines.sort(key=lambda line: line['at'], reverse=True)
    return lines


def _feed_lines(group_rows, rows, people):
    lines = []
    for r in group_rows:
        x = r['extra_data']
        label = FEEDERS[x['action']][1]
        queue = x.get('queue') or x.get('subject_key')
        if x['action'] == 'front_decision':
            title = f'Front · {short_name(queue)}' if queue else label
            detail, who = str(x.get('candidate') or r['message']), 'front'
        else:
            events = x.get('events')
            title, who = label, label
            detail = (f"{x.get('task') or ''}: {x.get('jobs')} jobs"
                      + (f', {int(events):,} events' if events else ''))
        lines.append(_line(r, people, title=title, detail=detail, who=who))
    return lines


def _node_guard_lines(group_rows, rows, people):
    ranks = {}
    if group_rows:
        from monitor_app.panda.node_guard import exclusion_ranks
        try:
            ranks = exclusion_ranks()
        except Exception:
            logger.exception('curated live page: node guard blacklist unreadable')
    lines = []
    for r in group_rows:
        fields = site_node_guard_fields(r['extra_data'], ranks)
        lines.append(_line(r, people, title=_node_guard_name(fields),
                           detail=node_guard_text(fields), who='node guard'))
    return lines


def _assessment_lines(group_rows, rows, people):
    lines = []
    for r in group_rows:
        x = r['extra_data']
        kind = str(x.get('assessment_kind') or x.get('subject_type') or '')
        verdict = str(x.get('verdict') or '').replace('_', ' ')
        report = str(x.get('report_title') or x.get('subject_key') or '')
        if verdict and verdict not in report.replace('_', ' '):
            report += f': {verdict}'
        line = _line(r, people,
                     title=f"Assessment · {ASSESSMENT_KINDS.get(kind, kind.replace('_', ' '))}",
                     detail=report,
                     level={'warning': 'WARNING', 'error': 'ERROR', 'critical': 'ERROR'}
                     .get(str(x.get('severity') or ''), r['levelname']))
        line['path'] = x.get('report_path') or x.get('url')
        lines.append(line)
    return lines


def _plain(text):
    """Page text without the long dash some records carry."""
    return str(text or '').replace(' — ', ', ').replace('—', ', ')


def _level_rank(level):
    return {'CRITICAL': 3, 'ERROR': 2, 'WARNING': 1}.get(level, 0)


def _at(ts):
    """'10-28 07:05', Eastern Time."""
    return timezone.localtime(ts).strftime('%m-%d %H:%M')
