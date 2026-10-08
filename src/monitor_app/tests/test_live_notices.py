from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase

from monitor_app.live_notices import ROUTINE_SUCCESSES, select_notice
from monitor_app.notice_router import _matches


class LiveNoticeSelectionTests(TestCase):
    def setUp(self):
        self.row = SimpleNamespace(app_name='epicprod', instance_name='ops-agent',
                                   message='declared state sync failed: CRIC unavailable')
        self.failures = {}
        self.extra = dict(action='catalog_sync', outcome='ok',
                          username='nightly_cron', sublevel='high', live_default=True)

    def select(self, **changes):
        return select_notice(self.row, dict(self.extra, **changes), self.failures, {})

    def test_routine_successes_are_quiet(self):
        for action in ROUTINE_SUCCESSES:
            with self.subTest(action=action):
                self.assertIsNone(self.select(action=action))

    def test_maintenance_is_quiet_including_failures_and_recoveries(self):
        for action in ('stash_drain', 'storage_sweep', 'log_rescue'):
            with self.subTest(action=action):
                self.assertIsNone(self.select(action=action, summary='moved=2'))
                self.assertIsNone(self.select(action=action, outcome='partial', reason='x'))
                self.assertIsNone(self.select(action=action))

    def test_maintenance_live_override_still_publishes(self):
        notice = select_notice(self.row, dict(self.extra, action='stash_drain'),
                               self.failures, {'stash_drain': True})
        self.assertIsNotNone(notice)

    def test_actual_changes_inside_routine_passes_remain_visible(self):
        for action, summary in (
                ('catalog_import', 'csv: 2 new, 405 updated'),
                ('past_import', 'created=1 updated=5541 errors=0'),
                ('questionnaire_import', 'request questionnaire: 0 new, 1 updated, 76 unchanged'),
                ('association_sweep', 'checked=22 new=0 intaken=1 unmatched=3'),
                ('segfault_inventory', '3 crashed jobs, 3 rows added, 147 signatures (1 new)'),
                ('segfault_dig', 'automatic: 1 dug, 1 traced'),
                ('segfault_study', '1 studies queued (key); 0 traced signatures marked covered'),
                ('segfault_notice', '1 need a reproduction, 0 traced unread')):
            with self.subTest(action=action):
                self.assertIsNotNone(self.select(action=action, summary=summary))

    def test_rewritten_import_rows_are_not_new_information(self):
        self.assertIsNone(self.select(action='past_import', summary='created=0 updated=5541 errors=0'))
        self.assertIsNone(self.select(action='catalog_import', summary='csv: 0 new, 405 updated'))

    def test_operator_requested_operations_are_not_quieted(self):
        self.assertIsNotNone(self.select(username='wenaus'))

    def test_real_production_actions_and_assessments_remain_visible(self):
        for action in ('rucio_arrivals', 'es_closeout', 'assessment_register',
                       'dataset_expected_events_set', 'production_finding'):
            self.assertIsNotNone(self.select(action=action))

    def test_shadow_decisions_are_quiet_but_actual_exclusions_are_not(self):
        self.assertIsNone(self.select(action='node_guard_decision', outcome='would_exclude'))
        self.assertIsNotNone(self.select(action='node_guard_decision', outcome='excluded'))

    def test_first_failure_then_changed_cause_then_recovery(self):
        failure = dict(action='declared_state_sync', outcome='error',
                       username='declared_cron', reason='CRIC unavailable')
        self.assertIsNotNone(self.select(**failure))
        self.assertIsNone(self.select(**failure))
        self.assertIsNotNone(self.select(**dict(failure, reason='proxy expired')))
        recovered = self.select(action='declared_state_sync', username='declared_cron',
                                live_default=False, sublevel='low')
        self.assertEqual(recovered['operation'], 'declared_state_sync_recovered')
        self.assertIn('proxy expired', recovered['summary'])
        self.assertTrue(recovered['live_default'])
        self.assertEqual(self.failures, {})
        # A subsequent ordinary quiet success stays quiet at subscription selection.
        normal = self.select(action='declared_state_sync', username='declared_cron',
                             live_default=False, sublevel='low')
        sub = SimpleNamespace(event='*', filters={'live': True, 'sublevel': ['high', 'normal']})
        self.assertFalse(_matches(sub, self.row, 'declared_state_sync', normal, {}))

    def test_failure_state_survives_a_publisher_restart(self):
        self.select(outcome='timeout', reason='timed out')
        state = deepcopy(self.failures)
        self.assertIsNone(select_notice(self.row, dict(self.extra, outcome='timeout',
                                                      reason='timed out'), state, {}))

    def test_problem_identity_separates_subjects(self):
        self.assertIsNotNone(self.select(outcome='error', subject_key='a', reason='failed'))
        self.assertIsNotNone(self.select(outcome='error', subject_key='b', reason='failed'))

    def test_reason_falls_back_to_record_message(self):
        notice = self.select(outcome='error')
        self.assertIn('CRIC unavailable', notice['reason'])

    def test_original_record_is_unchanged(self):
        original = dict(self.extra, outcome='error')
        before = dict(original)
        select_notice(self.row, original, self.failures, {})
        self.assertEqual(original, before)

    def test_explicit_force_live_override_keeps_routine_posts(self):
        self.assertIsNotNone(select_notice(self.row, self.extra, {}, {'catalog_sync': True}))

    def test_force_quiet_override_also_applies_to_recovery(self):
        self.select(outcome='error', reason='failed')
        recovered = self.select()
        sub = SimpleNamespace(event='*', filters={'live': True})
        self.assertFalse(_matches(sub, self.row, 'catalog_sync', recovered, {'catalog_sync': False}))

    def test_other_logging_namespaces_are_unchanged(self):
        self.row.app_name = 'testbed'
        self.assertEqual(select_notice(self.row, self.extra, {}, {}), self.extra)
