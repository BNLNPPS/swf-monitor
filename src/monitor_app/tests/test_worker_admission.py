import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase

from monitor_app.worker_admission import outcome


class WorkerAdmissionTests(SimpleTestCase):
    def test_known_prejob_exit_is_declined_but_signal_alone_is_not(self):
        self.assertEqual(outcome('BNL_OSG_PanDA_1', 'failed', 73), 'declined')
        self.assertEqual(outcome('another_queue', 'failed', 73), 'failed')
        self.assertEqual(outcome('BNL_OSG_PanDA_1', 'failed', 143), 'failed')
        self.assertEqual(outcome('BNL_OSG_PanDA_1', 'cancelled', 143), 'cancelled')

    def reporter(self):
        path = Path(__file__).resolve().parents[3] / 'scripts' / 'harvester-reporter.py'
        spec = importlib.util.spec_from_file_location('admission_reporter_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_multi_image_capability_requires_fresh_active_allocator(self):
        reporter = self.reporter()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = root / 'image-policy.json'
            state = root / 'allocation-state.json'
            template = root / 'pilot.sdf'
            policy.write_text(json.dumps({'images': ['/cvmfs/one', '/cvmfs/three'], 'queue': 'OSG'}))
            template.write_text('environment = "EPICPROD_NATIVE_IMAGE=/cvmfs/one"\n'
                                '+EpicprodImagePolicyFile = "' + str(policy) + '"\n')
            record = {'mode': 'active', 'images': ['/cvmfs/one', '/cvmfs/three'],
                      'queue': 'OSG', 'observed_at': '2026-10-24T00:00:00Z',
                      'valid_until': (datetime.utcnow() + timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ')}
            state.write_text(json.dumps(record))
            self.assertEqual(reporter.pilot_dispatch(str(template))['images'], ['/cvmfs/one', '/cvmfs/three'])
            record['valid_until'] = '2020-01-01T00:00:00Z'
            state.write_text(json.dumps(record))
            self.assertEqual(reporter.pilot_dispatch(str(template))['mode'], 'unknown')

    def test_startup_evidence_includes_actual_slot_and_budget(self):
        reporter = self.reporter()
        with TemporaryDirectory() as tmp:
            stem = Path(tmp) / 'grid.123.0'
            stem.with_suffix('.0.err').write_text('ePIC runtime policy: PanDA queue=BNL_OSG_PanDA_1; execution queue=ComputeCanada-Fir; minimum=9000 s; source=default\nglidein life 4134 s is under the 9000 s a job needs; no job taken\n')
            stem.with_suffix('.0.out').write_text('ePIC native pilot: image /image, host fc20557, time, nesting off\n')
            stem.with_suffix('.0.log').write_text('Message from starter on slot1_2@glidein_123@fc20557:\nTimeSlotBusy (s) : 81\nTimeExecute (s) : 2\n')
            result = reporter.admission_evidence('https://logs/grid.123.0.log', [('https://logs', tmp)])
        self.assertEqual(result['execution_queue'], 'ComputeCanada-Fir')
        self.assertEqual(result['slot'], 'slot1_2@glidein_123@fc20557')
        self.assertEqual(result['remaining_s'], 4134)
        self.assertEqual(result['required_s'], 9000)
        self.assertEqual((result['busy_s'], result['execute_s']), (81, 2))

    def test_expected_refusal_is_not_an_operational_error_reason(self):
        reporter = self.reporter()
        row = ['1', '123.0', 'BNL_OSG_PanDA_1', 'failed', 'completed',
               '2026-10-24 01:00:00', 'returned non-zero 73', None, None, None, '73']
        with patch.object(reporter, 'query', return_value=[row]), \
             patch.object(reporter, 'log_roots', return_value=[]), \
             patch.object(reporter, 'condor_reason') as read:
            report = reporter.not_finished({}, '2026-10-24 01:01:00', {})
        self.assertEqual(report['workers'], 0)
        self.assertEqual(report['sites'], {})
        read.assert_not_called()
