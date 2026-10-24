from unittest.mock import patch

from django.test import SimpleTestCase

from monitor_app.error_corrections import reading
from monitor_app.panda.sql import extract_errors


class ErrorReadingTests(SimpleTestCase):
    def setUp(self):
        self.rules = patch('monitor_app.error_corrections._rules', return_value=[])
        self.rules.start()
        self.addCleanup(self.rules.stop)

    def test_known_patterns_use_only_the_evidence_in_the_record(self):
        cases = [
            ('ddm', 200, "bytes mismatch for 'x.log.tgz': 100!=200", 'Log file catalog registration'),
            ('pilot', 1213, 'Reached maxtime', 'runtime limit'),
            ('pilot', 1212, 'Memory cgroup out of memory: Killed process 42', 'memory limit'),
            ('pilot', 1326, 'No replica found (allow_lan=True, allow_wan=False)', 'WAN'),
            ('dispatcher', 100, 'lost heartbeat : time', 'unknown'),
            ('taskbuffer', 300, 'The worker was cancelled while the job was running : Condor HoldReason: Job disappeared from remote schedd', 'underlying batch cause not reported'),
            ('taskbuffer', 300, 'The worker was finished while the job was running : None', 'not recorded'),
            ('executor', 139, 'payload exited 139', 'segfault'),
            ('executor', 1, 'payload exited 1', 'does not identify the cause'),
        ]
        for component, code, diag, expected in cases:
            with self.subTest(component=component, code=code):
                self.assertIn(expected, reading(component, code, diag)['label'])
        self.assertIsNone(reading('pilot', 9999, 'unknown error'))

    def test_incidental_warning_does_not_override_payload_signal(self):
        result = reading('pilot', 1305, 'Failed to execute payload: group: unknown groupid 123',
                         {'139': {'n': 5, 'rep': 1}})
        self.assertEqual(result['label'], 'payload segfault')
        self.assertEqual(result['modes'][0]['count'], 5)
        self.assertNotIn('group', result['label'])

    def test_job_error_root_uses_shared_reading(self):
        errors = extract_errors({'piloterrorcode': 1305,
            'piloterrordiag': 'Failed to execute payload', 'transexitcode': '139'})
        self.assertEqual(errors[0]['correction']['label'], 'payload segfault')
        self.assertEqual(errors[1]['correction']['label'], 'payload segfault')


class DiagnosticFocusTests(SimpleTestCase):
    def test_pattern_drilldown_preserves_time_task_execution_queue_and_code(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import MagicMock
        from monitor_app.panda.queries import diagnose_jobs
        end = datetime(2026, 10, 24, tzinfo=timezone.utc)
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        with patch('monitor_app.panda.queries.connections', {'panda': conn}), \
             patch('monitor_app.panda.queries._bulk_destinationse', return_value={}):
            result = diagnose_jobs(site='BNL_OSG_PanDA_1', taskid='41051',
                execsite='SU-ITS', ended_after=end-timedelta(hours=1), ended_before=end,
                status='failed', error_component='pilot', error_code='1213')
        self.assertNotIn('error', result)
        sql, params = cursor.execute.call_args.args
        for clause in ('"endtime" > %s', '"endtime" <= %s',
                       '"destinationsite" = %s', '"piloterrorcode" = %s'):
            self.assertIn(clause, sql)
        self.assertIn('SU-ITS', params)
        self.assertIn('41051', params)
        self.assertIn(1213, params)
        self.assertNotIn('"modificationtime" >= %s', sql)
