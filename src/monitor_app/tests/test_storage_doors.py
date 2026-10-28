"""The storage door record's two readings the node guard depends on
(monitor_app/panda/storage_doors.py, site-canary docs/STORAGE_DOORS.md):
the spans a door was down, and the failures those spans excuse.

Dict tests over the spans; no door is touched and no cycle runs."""
from datetime import datetime, timedelta, timezone
import json
import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from monitor_app.panda import storage_doors
from monitor_app.log_presentation import present_log

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


class InterruptedDoorReport(SimpleTestCase):
    DOOR = 'root://write.example.org:1094'

    def output(self, *events):
        return ''.join(storage_doors.PROGRESS_PREFIX + json.dumps(event) + '\n'
                       for event in events)

    def test_timeout_bytes_keep_the_current_door_and_completed_checks(self):
        completed = {'phase': 'completed', 'rse': 'FIRST', 'door': self.DOOR,
                     'write': 'ok', 'stat': 'ok', 'delete': 'ok'}
        current = {'phase': 'certificate', 'rse': 'SECOND', 'door': self.DOOR,
                   'path': '/canary/probe'}
        output = (self.output(completed, current) + storage_doors.PROGRESS_PREFIX + '{').encode()
        report = storage_doors.failure_context(output, 'timed out after 300s')
        self.assertIn('SECOND at ' + self.DOOR, report['summary'])
        self.assertIn('reading the door certificate', report['summary'])
        self.assertEqual(report['completed_checks'], [completed])
        self.assertEqual(report['probe_path'], '/canary/probe')

    def test_catalog_interruption_does_not_name_a_door(self):
        report = storage_doors.failure_context(self.output(
            {'phase': 'catalog', 'catalog_url': 'https://catalog.example.org'}), 'SIGTERM')
        self.assertIn('reading the storage-door catalog', report['summary'])
        self.assertEqual(report['rse'], '')
        self.assertEqual(report['door'], '')

    def test_publication_interruption_does_not_blame_the_last_door(self):
        report = storage_doors.failure_context(self.output(
            {'phase': 'completed', 'rse': 'FIRST', 'door': self.DOOR},
            {'phase': 'publication'}), 'SIGTERM')
        self.assertEqual(report['rse'], '')
        self.assertIn('publishing the cycle results', report['summary'])
        self.assertEqual(len(report['completed_checks']), 1)

    def test_old_log_explains_missing_subject_without_changing_the_record(self):
        extra = {'action': 'storage_door_cycle', 'outcome': 'error',
                 'reason': 'killed by signal SIGTERM', 'duration_ms': 131466}
        log = SimpleNamespace(extra_data=extra.copy(), message='storage_door_cycle error')
        shown = present_log(log)
        self.assertIn('door address and check in progress were not recorded', shown['summary'])
        self.assertEqual(log.extra_data, extra)

    def test_completed_cycle_with_source_errors_keeps_its_original_summary(self):
        log = SimpleNamespace(extra_data={'action': 'storage_door_cycle',
            'outcome': 'error', 'probed': 1},
            message='storage door cycle: 1 of 4 doors probed; catalog read failed')
        self.assertEqual(present_log(log)['summary'], log.message)

    def run_probe(self, command, certificate):
        events = []
        with (patch.object(storage_doors, 'settings', return_value=storage_doors.DEFAULTS.copy()),
              patch.object(storage_doors, 'previous_state', return_value={}),
              patch.object(storage_doors, 'catalog_doors', return_value={
                  'TEST-RSE': {'door': self.DOOR, 'prefix': '/test'}}),
              patch('swf_epicprod.jlab_auth.door_access', return_value=(
                  self.DOOR, {'SECRET': 'not-for-the-log'}, 'test-credential')),
              patch('canary.doors.run_command', side_effect=command),
              patch('canary.doors.door_certificate', side_effect=certificate)):
            try:
                storage_doors.run_cycle(dry_run=True, force=True, progress=events.append)
            except KeyboardInterrupt:
                pass  # emulate a killed doer at the chosen blocking call
        return events

    def test_cycle_names_the_door_before_each_blocking_check(self):
        events = self.run_probe(lambda *a, **kw: (0, 'ok'), lambda *a: T0 + timedelta(days=90))
        self.assertEqual([e['phase'] for e in events],
                         ['catalog', 'credential', 'write', 'stat', 'delete', 'certificate', 'completed'])
        self.assertTrue(all(e['rse'] == 'TEST-RSE' and e['door'] == self.DOOR
                            for e in events[1:]))
        self.assertNotIn('not-for-the-log', json.dumps(events))

    def test_interruption_keeps_the_step_that_never_returned(self):
        def command(argv, *a, **kw):
            if argv[0] == 'xrdfs' and argv[2] == 'stat':
                raise KeyboardInterrupt
            return 0, 'ok'
        events = self.run_probe(command, lambda *a: None)
        report = storage_doors.failure_context(self.output(*events), 'killed by signal SIGTERM')
        self.assertIn('checking the probe file for TEST-RSE at ' + self.DOOR, report['summary'])
        self.assertEqual(report['completed_checks'], [])

    def agent_report(self, result=None, error=None, stopping=False):
        spec = importlib.util.spec_from_file_location('door_report_agent',
            Path(__file__).resolve().parents[3] / 'agents/epicprod_ops_agent.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        agent = SimpleNamespace(_stopping=stopping, logger=Mock(), _log_action=Mock(),
                                _derive_reason=module.EpicProdOpsAgent._derive_reason)
        with patch.object(module.subprocess, 'run', return_value=result, side_effect=error):
            module.EpicProdOpsAgent._do_storage_door_cycle(agent, {'created_by': 'test'})
        return agent._log_action.call_args.kwargs

    def test_agent_preserves_timeout_output_in_the_action_record(self):
        output = self.output({'phase': 'write', 'rse': 'TEST-RSE', 'door': self.DOOR}).encode()
        record = self.agent_report(error=subprocess.TimeoutExpired('probe', 300, output=output))
        self.assertEqual(record['outcome'], 'timeout')
        self.assertEqual(record['door'], self.DOOR)
        self.assertIn('TEST-RSE', record['message'])

    def test_agent_records_shutdown_only_when_it_is_shutting_down(self):
        result = subprocess.CompletedProcess('probe', -15, stdout=self.output(
            {'phase': 'certificate', 'rse': 'TEST-RSE', 'door': self.DOOR}), stderr='')
        record = self.agent_report(result=result, stopping=True)
        self.assertIn('ops agent was shutting down', record['reason'])
        self.assertEqual(record['door'], self.DOOR)
        record = self.agent_report(result=result, stopping=False)
        self.assertEqual(record['reason'], 'killed by signal SIGTERM')
