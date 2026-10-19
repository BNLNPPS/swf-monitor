"""Reproduction attempts: a request the agent did not run reads as
withdrawn, never as queued (monitor_app/reproductions.py). Pure rows,
no store."""
from types import SimpleNamespace

from django.utils import timezone

from monitor_app.reproductions import _attempt, withdrawn_phase


SIG = SimpleNamespace(key='exit139:task39951', level='record', data={},
                      trace={}, exit_code=139)


def _entry(**over):
    e = {'request_id': 'fe2c58a2c862', 'run_id': None, 'pandaid': 2726603,
         'queue': 'UM_GREX_PanDA_1', 'role': 'elsewhere', 'requested_at':
         '2026-09-15T06:49:15+00:00', 'requested_by': 'swf-2',
         'outcome': 'submitted', 'jedi_task_id': None, 'canary_pandaid': None}
    e.update(over)
    return e


def test_request_in_the_agents_hands_reads_queued():
    row = _attempt(SIG, _entry(), None, timezone.now())
    assert (row['phase'], row['result'], row['active']) == ('queued_submission', 'pending', True)
    assert withdrawn_phase(_entry()) is None


def test_dropped_duplicate_reads_cancelled_and_ended():
    e = _entry(outcome='cancelled', withdrawn='duplicate',
               reason='duplicate: the same row on this queue was already in flight')
    row = _attempt(SIG, e, None, timezone.now())
    assert (row['phase'], row['result'], row['active']) == ('cancelled', 'cancelled', False)
    assert row['reason'].startswith('duplicate:')
    assert row['request_mirror'] == 'cancelled'


def test_failed_dispatch_reads_failed_submission():
    e = _entry(outcome='cancelled', withdrawn='dispatch_failed',
               reason='submission failed: payload-canary rc=2: no such queue')
    row = _attempt(SIG, e, None, timezone.now())
    assert (row['phase'], row['result'], row['active']) == ('submission_failed', 'inconclusive', False)
    assert row['reason'].startswith('submission failed:')


def test_an_ended_outcome_without_the_withdrawn_mark_is_left_to_the_run():
    # The reconcile writes an ended outcome only from a run; the row's
    # phase then comes from the run, not from the entry.
    assert withdrawn_phase(_entry(outcome='completed')) is None
