"""The storage door record's two readings the node guard depends on
(monitor_app/panda/storage_doors.py, site-canary docs/STORAGE_DOORS.md):
the spans a door was down, and the failures those spans excuse.

Dict tests over the spans; no door is touched and no cycle runs."""
from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from monitor_app.panda import storage_doors

T0 = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def span(start_min, end_min, rse='BNL-XRD'):
    return {'rse': rse, 'door': 'root://epicxrd1.sdcc.bnl.gov:1094',
            'start': T0 + timedelta(minutes=start_min),
            'end': None if end_min is None else T0 + timedelta(minutes=end_min)}


def row(minutes, error='pilot 1305', status='failed', queue='NERSC_Perlmutter_epic'):
    return {'queue': queue, 'host': 'nid006057', 'jobstatus': status,
            'jeditaskid': 40052, 'duration_s': 740.0,
            'endtime': (T0 + timedelta(minutes=minutes)).isoformat(),
            'error': error}


class SetAsideDoor(SimpleTestCase):
    def test_a_registration_failure_inside_a_span_is_set_aside(self):
        kept, by_queue = storage_doors.set_aside_door([row(30)], [span(0, 60)])
        self.assertEqual(kept, [])
        self.assertEqual(by_queue, {'NERSC_Perlmutter_epic': 1})

    def test_the_same_failure_outside_the_span_stands(self):
        kept, by_queue = storage_doors.set_aside_door([row(90)], [span(0, 60)])
        self.assertEqual(len(kept), 1)
        self.assertEqual(by_queue, {})

    def test_a_span_still_open_covers_everything_after_it(self):
        kept, _ = storage_doors.set_aside_door([row(600)], [span(0, None)])
        self.assertEqual(kept, [])

    def test_another_failure_in_the_span_stands(self):
        """A node killing jobs for its own reasons during an outage still
        reads as what it is."""
        kept, by_queue = storage_doors.set_aside_door(
            [row(30, error='pilot 1201')], [span(0, 60)])
        self.assertEqual(len(kept), 1)
        self.assertEqual(by_queue, {})

    def test_the_payloads_own_registration_exit_counts_too(self):
        for error in ('exe 78', 'trans 78'):
            kept, _ = storage_doors.set_aside_door([row(30, error=error)], [span(0, 60)])
            self.assertEqual(kept, [], error)

    def test_a_finished_job_is_never_set_aside(self):
        kept, _ = storage_doors.set_aside_door(
            [row(30, error='', status='finished')], [span(0, 60)])
        self.assertEqual(len(kept), 1)

    def test_no_spans_set_nothing_aside(self):
        """Before the canary has watched a door there is nothing to
        excuse, and the guard judges as it always did."""
        rows = [row(30), row(90)]
        kept, by_queue = storage_doors.set_aside_door(rows, [])
        self.assertEqual(kept, rows)
        self.assertEqual(by_queue, {})

    def test_a_row_without_an_end_time_stands(self):
        kept, _ = storage_doors.set_aside_door(
            [dict(row(30), endtime=None)], [span(0, 60)])
        self.assertEqual(len(kept), 1)

    def test_counts_are_per_queue(self):
        rows = [row(10), row(20), row(30, queue='BNL_OSG_PanDA_1')]
        _, by_queue = storage_doors.set_aside_door(rows, [span(0, 60)])
        self.assertEqual(by_queue, {'NERSC_Perlmutter_epic': 2, 'BNL_OSG_PanDA_1': 1})
