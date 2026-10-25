"""A running snapshot must never be presented as a preemption or extended in time."""
import os
import unittest

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
import django
django.setup()

from monitor_app.panda.queries import es_record_report, es_slot_timeline
from monitor_app.es_plot import slot_plot_svg


class SnapshotTimelineTest(unittest.TestCase):
    def setUp(self):
        self.record = {
            'started_at': 1000, 'written_at': 1300, 'slots': 2,
            'done': [{'unit_id': 'saved', 'slot': 0, 'started_at': 1010,
                      'ended_at': 1100, 'events': 10, 'close': 1}],
            'in_flight': [{'unit_id': 'working', 'slot': 0, 'started_at': 1200, 'events': 10}],
            'awaiting_close': [{'unit_id': 'pending', 'slot': 1, 'started_at': 1010,
                                'ended_at': 1250, 'events': 10}],
            'closes': [{'index': 1, 'ok': True, 'events': 10,
                        'started_at': 1100, 'ended_at': 1120}],
            'closing': [{'index': 2, 'units': ['pending'], 'started_at': 1280}],
        }
        self.job = {'starttime': '1970-01-01T00:16:40Z', 'endtime': None}

    def test_running_snapshot_has_no_cut_and_stops_at_observation(self):
        es = es_record_report(self.record, 'running')
        timeline = es_slot_timeline(self.job, es)
        self.assertIsNone(es['preempted'])
        self.assertEqual(es['ranges_interrupted'], [])
        self.assertEqual(es['ranges_unshipped'], [])
        self.assertEqual(timeline['wall_s'], 300)
        self.assertIsNone(timeline['cut_s'])
        self.assertTrue(timeline['snapshot_stale'])
        self.assertIn('running', {u['status'] for row in timeline['rows'] for u in row['units']})
        svg = slot_plot_svg(timeline)
        self.assertIn('close 2: running', svg)
        self.assertNotIn('preempted at', svg)

    def test_lost_job_preserves_saved_work_and_marks_only_unsaved_work(self):
        es = es_record_report(self.record, 'finished', loss_confirmed=True)
        job = dict(self.job, endtime='1970-01-01T00:30:00Z')
        timeline = es_slot_timeline(job, es)
        self.assertEqual(len(es['ranges_done']), 1)
        self.assertEqual(es['preempted']['unshipped_events'], 10)
        self.assertEqual(es['preempted']['interrupted_events'], 10)
        self.assertEqual(timeline['cut_s'], 300)
        self.assertEqual(timeline['wall_s'], 300)
        self.assertFalse(timeline['live'])

    def test_normal_harness_end_is_not_a_lost_node(self):
        es = es_record_report(dict(self.record, ended_at=1300), 'finished')
        self.assertIsNone(es['preempted'])
        self.assertFalse(es['live'])

    def test_lost_worker_observation_outlives_stale_panda_endtime(self):
        es = es_record_report(self.record, 'finished', loss_confirmed=True)
        job = dict(self.job, endtime='1970-01-01T00:16:55Z')
        timeline = es_slot_timeline(job, es)
        self.assertEqual(timeline['wall_s'], 300)
        self.assertGreaterEqual(timeline['tail_idle_s'], 0)
        self.assertLessEqual(timeline['busy_fraction'], 1)
        self.assertTrue(all(u['end_s'] <= timeline['wall_s']
                            for row in timeline['rows'] for u in row['units']))

    def test_missing_final_report_is_not_evidence_of_preemption(self):
        es = es_record_report(self.record, 'finished')
        timeline = es_slot_timeline(dict(self.job, endtime='1970-01-01T00:16:55Z'), es)
        self.assertTrue(timeline['incomplete'])
        self.assertIsNone(timeline['cut_s'])
        self.assertEqual(timeline['wall_s'], 300)
        self.assertEqual(es['ranges_interrupted'], [])
        self.assertEqual(es['ranges_unshipped'], [])
        self.assertTrue(all(u['status'] not in ('interrupted', 'unshipped')
                            for row in timeline['rows'] for u in row['units']))


if __name__ == '__main__':
    unittest.main()
