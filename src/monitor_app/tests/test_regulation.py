from datetime import datetime, timedelta, timezone
from django.test import SimpleTestCase
from monitor_app.comp_snapshot import effective_waiting_limit, _pilot_supply_part
from monitor_app.segfaults import matching_failure_finding


class RegulationTests(SimpleTestCase):
    def test_known_memory_failure_does_not_exempt_another_error_or_stage(self):
        rules = [{'fid': 'f-15', 'memory': True, 'exit_code': 137, 'stage': 'simu'}]
        self.assertEqual(matching_failure_finding(rules, memory=True, exit_code=137, stage='simulation'), 'f-15')
        self.assertIsNone(matching_failure_finding(rules, memory=False, exit_code=78, stage='simulation'))
        self.assertIsNone(matching_failure_finding(rules, memory=True, exit_code=137, stage='reconstruction'))

    def test_fixed_limit_is_current_but_old_dynamic_limit_is_unknown(self):
        now = datetime.now(timezone.utc)
        old = {'queue_limit': 200, 'at': (now - timedelta(hours=2)).isoformat()}
        self.assertEqual(effective_waiting_limit({'n_queue_limit_worker_ratio': '0',
                          'n_queue_limit_worker': '600'}, old, now), 600)
        self.assertIsNone(effective_waiting_limit({'n_queue_limit_worker_ratio': '30'}, old, now))
        self.assertEqual(_pilot_supply_part(None, 1000, 0, 0)['level'], 'amber')

    def test_failed_count_or_stale_demand_cannot_be_green(self):
        p = {'report_age_seconds': 1, 'waiting': 600, 'limit': 600,
             'error': 'worker count query failed'}
        self.assertEqual(_pilot_supply_part(p, 1000, 10, 10)['level'], 'amber')
        p.update(error=None, demand_age_seconds=120, demand_max_age_seconds=90)
        self.assertEqual(_pilot_supply_part(p, 1000, 10, 10)['level'], 'amber')
