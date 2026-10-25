"""Ratios, incident comparisons and history replacement; no database writes."""
from datetime import date
from unittest.mock import patch

from django.test import SimpleTestCase

from monitor_app.compute_efficiency import build, display_data, merge_days


class ComputeEfficiencyTests(SimpleTestCase):
    def data(self, rows, **options):
        with patch('monitor_app.compute_efficiency._queue_labels', return_value={'grex': 'GREX'}):
            return display_data({'daily': rows}, date(2026, 5, 1), date(2026, 6, 30), **options)

    def test_ratio_uses_summed_hours_and_preserves_real_zero(self):
        result = self.data([
            ['2026-05-01', 'grex', False, '', 90, 100, 10, 0, 0, 0, 0, 0],
            ['2026-05-02', 'grex', False, '', 0, 900, 30, 0, 0, 0, 0, 0],
            ['2026-06-02', 'grex', False, '', 0, 60, 2, 0, 0, 0, 0, 0],
        ])
        may, june = result['cells']
        self.assertEqual(may['efficiency'], 9)
        self.assertEqual(may['jobs'], 40)
        self.assertEqual(june['efficiency'], 0)

    def test_incident_exclusion_subtracts_hours_and_attempt_counts(self):
        row = ['2026-05-03', 'grex', False, '', 200, 1000, 120, 4, 100, 800, 100, 3]
        cell = self.data([row], exclude=True)['cells'][0]
        self.assertEqual((cell['finished'], cell['ended'], cell['jobs'], cell['missing']),
                         (100, 200, 20, 1))
        self.assertEqual(cell['efficiency'], 50)

    def test_unknown_execution_site_and_low_volume_are_visible(self):
        cell = self.data([
            ['2026-05-04', 'BNL_OSG_EPIC_PROD_1', True, '', 20, 40, 5, 1, 0, 0, 0, 0],
        ])
        self.assertIn('unresolved', cell['sites'][0]['label'].lower())
        self.assertIsNone(cell['cells'][0]['efficiency'])
        self.assertEqual(cell['cells'][0]['missing'], 1)

    def test_weeks_start_monday_and_same_osg_site_combines_queues(self):
        rows = [
            ['2026-05-04', 'BNL_OSG_EPIC_PROD_1', True, 'site:ComputeCanada-Fir',
             70, 100, 2, 0, 0, 0, 0, 0],
            ['2026-05-10', 'BNL_OSG_PanDA_1', True, 'site:ComputeCanada-Fir',
             30, 100, 2, 0, 0, 0, 0, 0],
        ]
        result = self.data(rows, bucket='week')
        self.assertEqual(len(result['sites']), 1)
        self.assertEqual(result['sites'][0]['label'], 'OSG · Fir')
        self.assertEqual(result['cells'][0]['period'], '2026-05-04')
        self.assertEqual(result['cells'][0]['efficiency'], 50)

    def test_refresh_removes_obsolete_recent_rows_but_retains_history(self):
        old = [['2026-05-01', 'historic'], ['2026-06-01', 'removed']]
        recent = [['2026-06-02', 'new']]
        self.assertEqual(merge_days(old, recent, date(2026, 6, 1)), [old[0], recent[0]])
        self.assertEqual(merge_days(old, recent, date(2026, 5, 1)), recent)

    def test_full_reconciliation_retains_expired_archive_periods(self):
        old = ['2026-05-01', 'historic']
        recent = ['2026-06-02', 'new']
        with patch('monitor_app.compute_efficiency.CachedProduct') as store, \
                patch('monitor_app.compute_efficiency.query_days', return_value=[recent]):
            store.objects.filter.return_value.first.return_value.value = {'daily': [old]}
            self.assertEqual(build(full=True)['daily'], [old, recent])

    def test_empty_full_source_cannot_erase_stored_history(self):
        with patch('monitor_app.compute_efficiency.CachedProduct') as store, \
                patch('monitor_app.compute_efficiency.query_days', return_value=[]):
            store.objects.filter.return_value.first.return_value.value = {'daily': [['2026-05-01']]}
            with self.assertRaisesRegex(RuntimeError, 'no source history'):
                build(full=True)
