"""Reproduction runs: every attempt to run a crashed row again, as one
read-only summary shared by the catalog, the runs page and the
signature page (swf-epicprod docs/SEGFAULT_DIAGNOSIS.md, Reproduction).

Two records make an attempt. The request lives on the signature
(``CrashSignature.reproduction``, one entry per queue the operator
asked for, written by ``segfaults.reproduce``); the execution lives in
the canary store (``ProbeRun`` of kind ``payload`` tagged with the
signature, written by the canary agent and brought up to date by the
probe collection). A request that has not produced a run is still an
attempt, and a signature-tagged run whose request mirror is stale is
still an attempt; the two are joined here, one row per attempt, and
never on a page read: ``attempts`` reads both stores and writes
nothing, ``reconcile`` writes the join back onto the signature and is
run by the canary agent after each collection.

Execution and result are kept apart. The execution phase says where
the job is (queued for submission, submitting, queued, running,
finishing, finished, failed, cancelled, submission failed); the result
says what the payload did (pending, awaiting report, crash reproduced,
completed without crash, inconclusive with the reason). A finished
PanDA job is not a successful payload: the canary wrapper exits 0 so
the pilot keeps the report, and the result comes from the payload's
own exit code in that report, or from the job digest when the job
failed after the payload ran.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone

from django.db import transaction
from django.utils import timezone

from .models import CrashSignature

logger = logging.getLogger(__name__)

REFERENCE_QUEUE = 'BNL_NPPS_GPU'
CRASH_EXITS = {134, 135, 136, 139}


def entry_role(entry, sig=None):
    """The role of a request's run: as recorded on the request (since
    2026-09-13), else by the legacy rule, the reference queue against
    everything else. ``production`` is the queue the crash happened on;
    ``reference`` and ``elsewhere`` are the other run of the pair."""
    role = (entry or {}).get('role')
    if role:
        return role
    return 'reference' if (entry or {}).get('queue') == REFERENCE_QUEUE else 'production'

# Execution phases, in the order the page lists them; active first.
PHASES = [
    ('queued_submission', 'Queued for submission'),
    ('submitting', 'Submitting'),
    ('queued', 'Queued'),
    ('running', 'Running'),
    ('finishing', 'Finishing'),
    ('finished', 'Finished'),
    ('failed', 'Failed'),
    ('cancelled', 'Cancelled'),
    ('submission_failed', 'Submission failed'),
    ('unknown', 'Unknown'),
]
PHASE_LABELS = dict(PHASES)
PHASE_ORDER = [p for p, _ in PHASES]
ACTIVE_PHASES = {'queued_submission', 'submitting', 'queued', 'running', 'finishing'}

RESULTS = [
    ('pending', 'Pending'),
    ('awaiting_report', 'Awaiting report'),
    ('crashed', 'Crash reproduced'),
    ('crash_observed', 'Crash observed'),
    ('completed', 'Completed without crash'),
    ('inconclusive', 'Inconclusive'),
    ('cancelled', 'Cancelled'),
]
RESULT_LABELS = dict(RESULTS)
RESULT_ORDER = [r for r, _ in RESULTS]
# A request's outcome words once its run has ended (nothing moves them
# back), and the subset that takes part in the pair settling the
# signature's outcome: a cancelled run is a withdrawn request, not a
# result, and counts for nothing.
ENDED_OUTCOMES = ('crashed', 'completed', 'inconclusive', 'cancelled')
PAIR_OUTCOMES = ('crashed', 'completed', 'inconclusive')

# The house state colours (static/css/state-colors.css) a phase or
# result cell takes, where its own word has no fill class.
PHASE_STATE = {'queued_submission': 'waiting', 'submission_failed': 'failed'}
RESULT_STATE = {'awaiting_report': 'pending', 'crashed': 'failed',
                'crash_observed': 'failed',
                'inconclusive': 'degraded'}

# PanDA job states before the job runs; the raw state is kept beside
# the phase.
QUEUED_JOB_STATES = ('defined', 'waiting', 'assigned', 'activated', 'pending',
                     'sent', 'starting', 'throttled')
FINISHING_JOB_STATES = ('holding', 'transferring', 'merging')
# The tolerance the legacy join allows between a request and the run
# it produced (the agent stamps the run when it dispatches).
LEGACY_JOIN_TOLERANCE = timedelta(seconds=5)


def new_request_id():
    """The immutable identity a request carries into the message and the
    run's data, so the join is exact from now on."""
    return uuid.uuid4().hex[:12]


def _iso(dt):
    return dt.isoformat(timespec='seconds') if dt else None


def _parse_dt(text):
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt


# ------------------------------------------------------------------ the join

def _signature_runs(keys=None):
    """The signature-tagged payload runs, by signature key, oldest first."""
    from canary.store.models import ProbeRun
    qs = (ProbeRun.objects.filter(data__kind='payload')
          .exclude(data__signature__isnull=True)
          .select_related('queue').order_by('submitted_at', 'id'))
    if keys is not None:
        qs = qs.filter(data__signature__in=list(keys))
    runs = list(qs)
    # Reports filed by the existing report sweep also carry fatal evidence
    # when the pilot's last heartbeat predates the signal. Read in bulk;
    # pages never fetch remote logs or mutate the canary store.
    from .models import EpicProdJob
    ids = [(r.data or {}).get('pandaid') for r in runs]
    reports = {j.pandaid: ((j.data or {}).get('payload_report') or {}).get('report') or {}
               for j in EpicProdJob.objects.filter(pandaid__in=[i for i in ids if i]).only('pandaid', 'data')}
    by_key = {}
    for run in runs:
        fatal = reports.get((run.data or {}).get('pandaid'), {}).get('fatal')
        if fatal:
            run.data = dict(run.data or {}, fatal=fatal)
        key = (run.data or {}).get('signature')
        if key:
            by_key.setdefault(key, []).append(run)
    return by_key


def join_requests(entries, runs):
    """Pair a signature's request entries with its runs, deterministically
    and one to one. Returns ``(pairs, unmatched_runs)`` where ``pairs`` is
    a list of ``(entry, run_or_None)`` in the entries' order.

    An entry that already names its run (``run_id``) keeps it; an entry
    carrying a ``request_id`` takes the run stamped with it; a legacy
    entry without either takes the earliest run on its queue for its
    crashed job submitted at or after its request time, among the runs
    no other entry holds. Runs held by earlier entries, settled or not,
    are never reassigned, and a run never serves two entries.
    """
    by_id = {str(r.id): r for r in runs}
    by_request = {}
    for r in runs:
        rid = (r.data or {}).get('request_id')
        if rid:
            by_request.setdefault(rid, r)
    claimed = set()
    pairs = [None] * len(entries)
    # Exact identities first, so a legacy match never takes a run that
    # belongs to a later request by identity.
    for i, e in enumerate(entries):
        run = None
        if e.get('run_id') and str(e['run_id']) in by_id:
            run = by_id[str(e['run_id'])]
        elif e.get('request_id') and e['request_id'] in by_request:
            run = by_request[e['request_id']]
        if run is not None and run.id not in claimed:
            claimed.add(run.id)
            pairs[i] = (e, run)
    # Then the legacy entries, in request order.
    order = sorted((i for i, p in enumerate(pairs) if p is None),
                   key=lambda i: (_parse_dt(entries[i].get('requested_at'))
                                  or datetime.min.replace(tzinfo=dt_timezone.utc), i))
    for i in order:
        e = entries[i]
        if e.get('request_id'):
            # An exact identity with no run yet: the request is still in
            # the agent's hands (or its dispatch failed before a run was
            # recorded); a legacy match would be a guess.
            pairs[i] = (e, None)
            continue
        requested = _parse_dt(e.get('requested_at')) or datetime.min.replace(tzinfo=dt_timezone.utc)
        run = next((r for r in runs
                    if r.id not in claimed
                    and r.queue.name == e.get('queue')
                    and (r.data or {}).get('reproduction_of') == e.get('pandaid')
                    and not (r.data or {}).get('request_id')
                    and r.submitted_at >= requested - LEGACY_JOIN_TOLERANCE), None)
        if run is not None:
            claimed.add(run.id)
        pairs[i] = (e, run)
    unmatched = [r for r in runs if r.id not in claimed]
    return pairs, unmatched


# ---------------------------------------------------------- phase and result

def phase_of(run):
    """The execution phase of a run (None: a request with no run yet)."""
    if run is None:
        return 'queued_submission', ''
    d = run.data or {}
    st = run.status
    js = d.get('job_status') or ''
    if st == 'failed_submit':
        return 'submission_failed', ''
    if run.jeditaskid is None:
        return 'submitting', ''
    # Terminal evidence from the job wins over the run's own flag: a run
    # still marked submitted whose job has ended is finished or failed,
    # awaiting collection.
    if js == 'finished':
        return 'finished', js
    if js == 'failed':
        return 'failed', js
    if js in ('cancelled', 'closed'):
        return 'cancelled', js
    if st in ('collected', 'finished'):
        return 'finished', js
    if st == 'failed':
        task_state = d.get('task_status') or ''
        return ('cancelled' if task_state in ('aborted', 'broken') else 'failed'), js or task_state
    if js == 'running':
        return 'running', js
    if js in FINISHING_JOB_STATES:
        return 'finishing', js
    if js in QUEUED_JOB_STATES or not js:
        return 'queued', js
    return 'unknown', js


def result_of(run, phase):
    """The reproduction result of a run given its phase: what the payload
    did, never what the pilot did."""
    if run is None:
        return 'pending', '', None
    d = run.data or {}
    if phase == 'submission_failed':
        reason = d.get('error') or (d.get('stderr') or d.get('stdout') or '').strip().splitlines()[-1:]
        reason = reason if isinstance(reason, str) else (reason[0] if reason else 'submission failed')
        return 'inconclusive', f'submission failed: {reason}'[:300], None
    fatal = d.get('fatal') or {}
    if fatal.get('signal') in (6, 7, 8, 11):
        names = {6: 'SIGABRT', 7: 'SIGBUS', 8: 'SIGFPE', 11: 'SIGSEGV'}
        reason = f"{names[fatal['signal']]} observed in {fatal.get('stage') or 'payload'}"
        if fatal.get('terminated'):
            reason += '; stalled after the signal; stage terminated by watchdog'
        elif phase in ACTIVE_PHASES:
            reason += '; execution has not ended'
        else:
            reason += '; execution ended separately'
        # An observed signal is evidence, not an invented process exit or
        # proof that the requested crashing frame was reached.
        return 'crash_observed', reason, d.get('payload_exit_code')
    if phase == 'cancelled':
        # A killed run is a withdrawn request, not a result.
        return 'cancelled', 'request withdrawn', None
    if phase in ACTIVE_PHASES:
        return 'pending', '', None
    if run.status == 'collected':
        rc = d.get('payload_exit_code')
        if rc in CRASH_EXITS:
            return 'crashed', '', rc
        if rc == 0:
            return 'completed', '', rc
        return 'inconclusive', f'payload exited {rc}', rc
    if run.status == 'finished':
        return 'inconclusive', d.get('collect_note') or 'finished without a payload report', None
    if run.status == 'failed':
        errors = d.get('errors') or {}
        parts = [f"{k} {v.get('code')}" for k, v in errors.items()] if isinstance(errors, dict) else []
        return 'inconclusive', 'the canary job failed' + (': ' + ', '.join(parts) if parts else ''), None
    # Ended, not yet collected.
    return 'awaiting_report', '', None


# ---------------------------------------------------------------- the rows

def withdrawn_phase(entry):
    """The phase and result of a request the agent did not run, as the
    agent recorded on the entry (``withdraw_request``): ``cancelled`` for
    a duplicate it dropped, ``submission_failed`` for a dispatch that
    failed before a run existed. None for a request still in the
    agent's hands."""
    withdrawn = (entry or {}).get('withdrawn')
    if not withdrawn or entry.get('outcome') != 'cancelled':
        return None
    if withdrawn == 'dispatch_failed':
        return 'submission_failed', 'inconclusive', entry.get('reason') or 'submission failed'
    return 'cancelled', 'cancelled', entry.get('reason') or 'request withdrawn'


def _attempt(sig, entry, run, now):
    """One attempt row from a request entry, its run, or both."""
    d = (run.data or {}) if run is not None else {}
    phase, job_status = phase_of(run)
    result, reason, rc = result_of(run, phase)
    withdrawn = withdrawn_phase(entry) if run is None else None
    if withdrawn:
        phase, result, reason = withdrawn
    queue = (run.queue.name if run is not None else entry.get('queue')) or ''
    started = _parse_dt(d.get('started_at'))
    ended = _parse_dt(d.get('ended_at'))
    created = _parse_dt(d.get('created_at')) or (run.submitted_at if run is not None else None)
    wait_s = d.get('wait_s')
    if wait_s is None and started and created:
        wait_s = int((started - created).total_seconds())
    run_s = d.get('run_s')
    running_for_s = None
    if run_s is None and started and ended:
        run_s = int((ended - started).total_seconds())
    if started and not ended and phase in ('running', 'finishing'):
        running_for_s = int((now - started).total_seconds())
    observed = _parse_dt(d.get('observed_at')) or (run.modified_at if run is not None else None)
    # Why the job itself failed, when it did: the pilot's or the DDM error
    # the collection recorded, or the note of a collection from the digest.
    execution_note = ''
    if phase in ('failed', 'cancelled'):
        errors = d.get('errors') or {}
        parts = [f"{k} {v.get('code')}: {(v.get('diag') or '')[:80]}".rstrip(': ')
                 for k, v in errors.items()] if isinstance(errors, dict) else []
        note = d.get('collect_note') or ''
        if note.startswith('payload outcome from the job digest; '):
            note = note[len('payload outcome from the job digest; '):]
        execution_note = '; '.join(parts) if parts else note
    requested = _parse_dt(entry.get('requested_at')) if entry else None
    if requested is None and run is not None:
        requested = run.submitted_at
    if run is not None:
        attempt_id = str(run.id)
    else:
        attempt_id = f"request:{sig.key}:{entry.get('request_id') or entry.get('requested_at') or ''}:{entry.get('queue')}"
    return {
        'id': attempt_id,
        'run_id': str(run.id) if run is not None else '',
        'request_id': (entry or {}).get('request_id') or d.get('request_id') or '',
        'signature': sig.key,
        'level': sig.level,
        'member_of': (sig.data or {}).get('member_of', ''),
        'frame': (sig.trace or {}).get('frame', ''),
        'exit_code': sig.exit_code,
        'original_pandaid': (entry or {}).get('pandaid') or d.get('reproduction_of'),
        'queue': queue,
        'role': entry_role(entry) if entry else ('reference' if queue == REFERENCE_QUEUE else 'production'),
        'requested_at': _iso(requested),
        'requested_by': (entry or {}).get('requested_by') or '',
        'jeditaskid': run.jeditaskid if run is not None else None,
        'canary_pandaid': d.get('pandaid'),
        'phase': phase,
        'phase_label': PHASE_LABELS.get(phase, phase),
        'phase_state': PHASE_STATE.get(phase, phase),
        'job_status': job_status,
        'active': phase in ACTIVE_PHASES,
        'execution_note': execution_note[:200],
        'result': result,
        'result_label': RESULT_LABELS.get(result, result),
        'result_state': RESULT_STATE.get(result, result),
        'reason': reason,
        'payload_exit_code': rc,
        'payload_stage': d.get('payload_stage') or '',
        'events_processed': d.get('events_processed'),
        'created_at': _iso(created),
        'started_at': _iso(started),
        'ended_at': _iso(ended),
        'wait_s': wait_s,
        'run_s': run_s,
        'running_for_s': running_for_s,
        'mem_limit_mb': (entry or {}).get('mem_limit_mb') or d.get('mem_limit_mb'),
        'container': (entry or {}).get('container') or d.get('container') or '',
        'landed_site': d.get('landed_site') or '',
        'host': d.get('host') or '',
        'observed_at': _iso(observed),
        'observed_age_s': int((now - observed).total_seconds()) if observed else None,
        'collect_error': d.get('collect_error') or '',
        'request_mirror': (entry or {}).get('outcome', '') if entry else '',
    }


def attempts(keys=None, signatures=None):
    """Every reproduction attempt, one row each, newest request first
    among the ended and the active ones ahead of them; reads the
    signature store and the canary store, writes nothing.

    ``keys`` restricts to those signatures (their own attempts; a
    trace-level signature's members are the caller's to add through
    ``member_keys``). ``signatures`` passes already loaded rows.
    """
    now = timezone.now()
    if signatures is None:
        qs = CrashSignature.objects.all()
        if keys is not None:
            qs = qs.filter(key__in=list(keys))
        signatures = list(qs)
    runs_by_key = _signature_runs(keys=[s.key for s in signatures] if keys is not None else None)
    rows = []
    for sig in signatures:
        entries = list(sig.reproduction or [])
        runs = runs_by_key.get(sig.key, [])
        if not entries and not runs:
            continue
        pairs, unmatched = join_requests(entries, runs)
        for entry, run in pairs:
            rows.append(_attempt(sig, entry, run, now))
        for run in unmatched:
            rows.append(_attempt(sig, None, run, now))
    # Active first, in request order; then the ended, newest first.
    active = [r for r in rows if r['active']]
    ended = sorted([r for r in rows if not r['active']],
                   key=lambda r: r['requested_at'] or '', reverse=True)
    return active + ended


def member_keys(sig_rows):
    """For catalog rows: the keys whose attempts a row shows, itself plus
    its members for a trace-level row (``data['members']``)."""
    out = {}
    for r in sig_rows:
        keys = [r['key']] + list(r.get('members') or [])
        out[r['key']] = keys
    return out


def counts_by_signature(rows, members=None):
    """Per-signature counts of active and ended attempts, each attempt
    counted once; a trace-level key gets its members' attempts too."""
    own = {}
    for r in rows:
        c = own.setdefault(r['signature'], {'active': 0, 'ended': 0, 'ids': set()})
        c['ids'].add(r['id'])
        c['active' if r['active'] else 'ended'] += 1
    out = {}
    for key, keys in (members or {}).items():
        seen, active, ended = set(), 0, 0
        for k in keys:
            c = own.get(k)
            if not c:
                continue
            for r in rows:
                if r['signature'] == k and r['id'] not in seen:
                    seen.add(r['id'])
                    if r['active']:
                        active += 1
                    else:
                        ended += 1
        out[key] = {'active': active, 'ended': ended}
    for key, c in own.items():
        out.setdefault(key, {'active': c['active'], 'ended': c['ended']})
    return out


# Where a signature stands on reproduction, for the catalog's facet: the
# settled outcomes, the runs in progress, and for the rest whether a run
# can be formed at all.
REPRODUCTION_STATES = [
    ('run_needed', 'Run needed'),
    ('in_progress', 'In progress'),
    ('not_runnable', 'Not runnable'),
    ('reproduced', 'Reproduced'),
    ('site_dependent', 'Site dependent'),
    ('not_reproduced', 'Not reproduced'),
]
REPRODUCTION_STATE_LABELS = dict(REPRODUCTION_STATES)
REPRODUCTION_STATE_ORDER = [k for k, _ in REPRODUCTION_STATES]
SETTLED_OUTCOMES = ('reproduced', 'site_dependent', 'not_reproduced')


def _settled(outcomes):
    """One settled outcome for a set of them: a crash reproduced anywhere
    settles the frame as reproduced."""
    outcomes = [o for o in outcomes if o in SETTLED_OUTCOMES]
    for o in SETTLED_OUTCOMES:
        if o in outcomes:
            return o
    return ''


def reproduction_states(sig_rows, counts):
    """The reproduction state of every catalog row (signature_summary
    rows carrying ``runnable``, ``reproduction_outcome``, ``member_of``
    and ``members``), given the per-row attempt counts. A signature
    settled by its own runs, or a member of a frame one of whose members
    settled, is settled; a signature with an attempt still active is in
    progress; otherwise a run is needed where one can be formed, and the
    signature is not runnable where it cannot. Returns key -> state."""
    by_key = {r['key']: r for r in sig_rows}
    frame_outcome = {}
    for r in sig_rows:
        if r.get('level') == 'trace':
            frame_outcome[r['key']] = _settled(
                [r.get('reproduction_outcome', '')]
                + [by_key[m].get('reproduction_outcome', '') for m in r.get('members') or [] if m in by_key])
    out = {}
    for r in sig_rows:
        c = counts.get(r['key']) or {'active': 0, 'ended': 0}
        own = r.get('reproduction_outcome', '')
        settled = _settled([own])
        if not settled and r.get('member_of') in frame_outcome:
            settled = frame_outcome[r['member_of']]
        if not settled and r.get('level') == 'trace':
            settled = frame_outcome.get(r['key'], '')
        if c['active']:
            out[r['key']] = 'in_progress'
        elif settled:
            out[r['key']] = settled
        else:
            runnable = r.get('runnable') or any(
                by_key[m].get('runnable') for m in r.get('members') or [] if m in by_key)
            out[r['key']] = 'run_needed' if runnable else 'not_runnable'
    return out


def global_counts(rows):
    return {'active': sum(1 for r in rows if r['active']),
            'ended': sum(1 for r in rows if not r['active']),
            'total': len(rows)}


def freshness(rows):
    """The latest observation among the rows, and its age."""
    latest = None
    for r in rows:
        t = _parse_dt(r.get('observed_at'))
        if t and (latest is None or t > latest):
            latest = t
    if latest is None:
        return {'observed_at': None, 'age_s': None}
    return {'observed_at': _iso(latest), 'age_s': int((timezone.now() - latest).total_seconds())}


# ----------------------------------------------------------- reconciliation

def _entry_outcome(row):
    """The request mirror's outcome word for an attempt row (the
    signature's own vocabulary: submitted, running, crashed, completed,
    inconclusive, cancelled)."""
    if row['result'] == 'crash_observed':
        # A fatal signal the watchdog ended with the stage's exit in the
        # crash class is the payload's crash (the trace comes from its
        # report); a signal with no payload exit stays evidence only.
        if row['active']:
            return 'running'
        return 'crashed' if row['payload_exit_code'] in CRASH_EXITS else 'inconclusive'
    if row['result'] in ENDED_OUTCOMES:
        return row['result']
    if row['phase'] in ('running', 'finishing', 'queued'):
        return 'running' if row['phase'] != 'queued' else 'submitted'
    if row['phase'] in ('finished', 'failed', 'cancelled'):
        return 'running'          # ended, report not yet collected
    return 'submitted'


def reconcile(sig_key, queue_diagnosis=None):
    """Write the join back onto one signature: each request entry gets
    its run's identity and outcome, and the pair settles the signature's
    reproduction outcome (reproduced, site_dependent, not_reproduced,
    inconclusive) once the production run and the other run of the pair
    (the reference queue or a second production-class queue) have both
    reported. A reproduced crash with no trace on record has its trace
    read from the crashed run's payload report (the other run first,
    the reference queue being the one of known conditions), so a reproduced crash yields its
    trace where the campaign's did not. The diagnosis is queued once
    when the outcome stands as reproduced or site dependent with a trace
    on record and no study yet. Runs under a row lock so a request
    appended meanwhile is kept; the bus message goes out after the lock.
    Returns what changed."""
    from .segfaults import covering_finding, trace_from_report
    changed = {'entries': 0, 'outcome': None, 'trace': None, 'diagnosis': False, 'covered': None}
    to_queue = None
    with transaction.atomic():
        sig = CrashSignature.objects.select_for_update().filter(key=sig_key).first()
        if sig is None or not sig.reproduction:
            return changed
        entries = list(sig.reproduction)
        runs = _signature_runs(keys=[sig.key]).get(sig.key, [])
        pairs, _unmatched = join_requests(entries, runs)
        now = timezone.now()
        for i, (entry, run) in enumerate(pairs):
            if run is None:
                continue
            row = _attempt(sig, entry, run, now)
            before = dict(entry)
            entry['run_id'] = str(run.id)
            entry['jedi_task_id'] = run.jeditaskid
            entry['canary_pandaid'] = row['canary_pandaid']
            outcome = _entry_outcome(row)
            if before.get('outcome') in ENDED_OUTCOMES and outcome in ('submitted', 'running'):
                # Never move a settled entry back on missing evidence.
                outcome = before['outcome']
            entry['outcome'] = outcome
            if outcome in ENDED_OUTCOMES:
                entry['exit_code'] = row['payload_exit_code']
                entry['minutes'] = (row['run_s'] / 60.0) if row['run_s'] else entry.get('minutes')
                entry['verdict_time'] = entry.get('verdict_time') or _iso(now)
                entry['reason'] = row['reason']
            if entry != before:
                changed['entries'] += 1
            entries[i] = entry
        settled = [e for e in entries if e.get('outcome') in PAIR_OUTCOMES]
        # The pair: the production run against the other run, whether on
        # the reference queue or a second production-class queue; a
        # cancelled request is not in it.
        prod = [e for e in settled if entry_role(e) == 'production']
        ref = [e for e in settled if entry_role(e) != 'production']
        outcome = None
        if prod and ref:
            p, r = prod[-1]['outcome'], ref[-1]['outcome']
            if p == 'crashed' and r == 'crashed':
                outcome = 'reproduced'
            elif p == 'crashed' and r == 'completed':
                outcome = 'site_dependent'
            elif p == 'completed' and r == 'completed':
                outcome = 'not_reproduced'
            elif p == 'completed' and r == 'crashed':
                outcome = 'reproduced'
            else:
                outcome = 'inconclusive'
        elif ref and ref[-1]['outcome'] == 'crashed':
            outcome = 'reproduced'
        if (outcome in ('reproduced', 'site_dependent')
                and (sig.trace or {}).get('trace_status') != 'found'):
            # The crashed run's report carries the stage's log tail
            # (payload 0.12 and later); record_trace saves the trace and
            # merges the frame under the lock held here.
            for entry in ref[::-1] + prod[::-1]:
                if entry.get('outcome') == 'crashed' and entry.get('canary_pandaid'):
                    if trace_from_report(sig, entry['canary_pandaid']):
                        changed['trace'] = entry['canary_pandaid']
                        # The merge under a frame entry wrote member_of on
                        # a fresh row instance; read it back so the
                        # covering finding is seen on this pass.
                        sig.refresh_from_db(fields=['data', 'status', 'trace'])
                        break
        data = dict(sig.data or {})
        fields = []
        if changed['entries']:
            sig.reproduction = entries
            fields.append('reproduction')
        if outcome and data.get('reproduction_outcome') != outcome:
            data['reproduction_outcome'] = outcome
            data['reproduction_settled_at'] = _iso(now)
            changed['outcome'] = outcome
            sig.data = data
            fields.append('data')
            if outcome in ('reproduced', 'site_dependent') and sig.status in ('reproducing', 'traced', 'new'):
                sig.status = 'reproduced'
                fields.append('status')
            elif outcome == 'not_reproduced' and sig.status == 'reproducing':
                sig.status = 'not_reproduced'
                fields.append('status')
        if (data.get('reproduction_outcome') in ('reproduced', 'site_dependent')
                and (sig.trace or {}).get('trace_status') == 'found'
                and not data.get('diagnosis')):
            # Marked before the message goes out, so a second pass never
            # queues the study twice; the trace may arrive after the
            # outcome settled, so this is checked on every pass. A frame
            # whose reading already exists as a finding owes no study:
            # the finding is the diagnosis and the signature is marked by
            # it, so nothing further is ever asked of it.
            finding = covering_finding(sig)
            if finding:
                data['diagnosis'] = {'state': 'covered', 'finding': finding['fid'],
                                     'anchor': finding['anchor'], 'requested_by': 'auto:reproduced',
                                     'submitted_at': _iso(now)}
                sig.verdict = f"covered by finding {finding['fid']}: {finding['title']}".strip(': ')
                sig.status = 'diagnosed'
                for f in ('verdict', 'status'):
                    if f not in fields:
                        fields.append(f)
                changed['covered'] = finding['fid']
            else:
                data['diagnosis'] = {'state': 'queued', 'requested_by': 'auto:reproduced',
                                     'submitted_at': _iso(now)}
                to_queue = sig.key
                changed['diagnosis'] = True
            sig.data = data
            if 'data' not in fields:
                fields.append('data')
        if fields:
            sig.save(update_fields=fields + ['updated_at'])
    if to_queue and queue_diagnosis is not None:
        queue_diagnosis(to_queue, 'auto:reproduced')
    return changed


def withdraw_request(sig_key, request_id, reason, failed=False):
    """Record on the signature that the canary agent did not run a
    request: dropped as a duplicate of a submission already in flight
    for the same row and queue, or a dispatch that failed before a run
    was recorded (``failed=True``). Either way the entry's outcome is
    ``cancelled``, a withdrawn request that counts for nothing in the
    pair; ``withdrawn`` says which (``duplicate`` or
    ``dispatch_failed``), and the runs page reads the second as a failed
    submission with the reason. Without this the request would read
    "queued for submission" forever and keep its signature open to every
    reconciliation pass. Nothing is written when a run already carries
    the request id: the run is the record then, and the reconciliation
    reads it. Returns what happened: ``withdrawn``, ``run exists``,
    ``already ended``, or ``no such request``."""
    with transaction.atomic():
        sig = CrashSignature.objects.select_for_update().filter(key=sig_key).first()
        if sig is None or not sig.reproduction:
            return 'no such request'
        entries = list(sig.reproduction)
        idx = next((i for i, e in enumerate(entries) if e.get('request_id') == request_id), None)
        if idx is None:
            return 'no such request'
        if entries[idx].get('outcome') in ENDED_OUTCOMES:
            return 'already ended'
        runs = _signature_runs(keys=[sig.key]).get(sig.key, [])
        if any((r.data or {}).get('request_id') == request_id for r in runs):
            return 'run exists'
        entry = dict(entries[idx])
        entry['outcome'] = 'cancelled'
        entry['reason'] = (reason or '')[:300]
        entry['withdrawn'] = 'dispatch_failed' if failed else 'duplicate'
        entry['verdict_time'] = _iso(timezone.now())
        entries[idx] = entry
        sig.reproduction = entries
        sig.save(update_fields=['reproduction', 'updated_at'])
    return 'withdrawn'


def open_signature_keys():
    """The signatures with a request not yet settled, or a run of theirs
    not yet mirrored: what a reconciliation pass visits."""
    keys = set()
    for sig in CrashSignature.objects.exclude(reproduction=[]).exclude(reproduction__isnull=True).only('key', 'reproduction'):
        if any(e.get('outcome') not in ENDED_OUTCOMES
               for e in (sig.reproduction or [])):
            keys.add(sig.key)
    return sorted(keys)


def reconcile_open(queue_diagnosis=None):
    """One pass over the open signatures; returns per-key changes."""
    out = {}
    for key in open_signature_keys():
        try:
            out[key] = reconcile(key, queue_diagnosis=queue_diagnosis)
        except Exception as e:                                # noqa: BLE001
            logger.error('reproduction reconcile %s failed: %s', key, e, exc_info=True)
            out[key] = {'error': str(e)[:300]}
    return out
