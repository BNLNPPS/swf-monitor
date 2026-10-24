import json

from django.test import SimpleTestCase

from monitor_app.panda.queries import _error_distribution_views


class PandaErrorDistributionTests(SimpleTestCase):
    def test_cross_site_checksum_pattern_preserves_correlations(self):
        views = _error_distribution_views([
            {
                'site': 'BNL_ePIC_GOOGLE',
                'taskid': 38511,
                'count': 5703,
                'representative_pandaid': 1669921,
            },
            {
                'site': 'BNL_OSG_EPIC_PROD_1',
                'taskid': 38512,
                'count': 2770,
                'representative_pandaid': 1690091,
            },
            {
                'site': 'UM_GREX_PanDA_1',
                'taskid': 38508,
                'count': 1779,
                'representative_pandaid': 1685001,
            },
            {
                'site': 'UM_GREX_PanDA_1',
                'taskid': 38509,
                'count': 1562,
                'representative_pandaid': 1685002,
            },
        ])

        self.assertTrue(views['multi_site'])
        self.assertEqual(
            views['site_counts'],
            [
                {'site': 'BNL_ePIC_GOOGLE', 'count': 5703},
                {'site': 'UM_GREX_PanDA_1', 'count': 3341},
                {'site': 'BNL_OSG_EPIC_PROD_1', 'count': 2770},
            ],
        )
        self.assertEqual(
            views['task_counts'][0],
            {'taskid': 38511, 'count': 5703},
        )
        self.assertEqual(
            views['representative_pandaids'],
            [1669921, 1690091, 1685001, 1685002],
        )

    def test_postgres_json_text_is_decoded_explicitly(self):
        views = _error_distribution_views(json.dumps([{
            'site': 'BNL_ePIC_GOOGLE',
            'taskid': 38511,
            'count': 5703,
            'representative_pandaid': 1669921,
        }]))

        self.assertFalse(views['multi_site'])
        self.assertEqual(
            views['site_counts'],
            [{'site': 'BNL_ePIC_GOOGLE', 'count': 5703}],
        )


class PandaErrorScopeTests(SimpleTestCase):
    def test_execution_queue_filters_patterns_and_status_counts(self):
        from unittest.mock import MagicMock, patch
        from monitor_app.panda.queries import error_summary
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        with patch('monitor_app.panda.queries.connections', {'panda': conn}), \
                patch('monitor_app.panda.queries._declared_failures', return_value=None):
            result = error_summary(site='BNL_OSG_PanDA_1',
                                   execsite='OSG_US_FSU_HNPGRID', status='failed')
        self.assertNotIn('error', result)
        self.assertEqual(result['filters']['execsite'], 'OSG_US_FSU_HNPGRID')
        self.assertEqual(cursor.execute.call_count, 2)
        for call in cursor.execute.call_args_list:
            sql, params = call.args
            self.assertEqual(sql.count('AND "destinationsite" = %s'), 2)
            self.assertEqual(params.count('OSG_US_FSU_HNPGRID'), 2)
            self.assertEqual(params.count('BNL_OSG_PanDA_1'), 2)

    def test_multi_queue_filter_is_applied_in_sql(self):
        from unittest.mock import MagicMock, patch
        from monitor_app.panda.queries import error_summary
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        with patch('monitor_app.panda.queries.connections', {'panda': conn}), \
                patch('monitor_app.panda.queries._declared_failures', return_value=None):
            error_summary(site='NERSC_Perlmutter_epic,NERSC_Perlmutter_epic_es')
        for call in cursor.execute.call_args_list:
            sql, params = call.args
            self.assertEqual(sql.count('AND "computingsite" IN (%s, %s)'), 2)
            self.assertEqual(params.count('NERSC_Perlmutter_epic_es'), 2)


class ErrorTaskFacetTests(SimpleTestCase):
    def test_task_counts_come_from_jobs_not_overlapping_error_patterns(self):
        from unittest.mock import MagicMock, patch
        from monitor_app.panda.queries import error_summary
        cursor = MagicMock()
        cursor.fetchall.side_effect = [[], [('failed', 40856, 131),
                                           ('failed', 40856, 21),
                                           ('closed', 41000, 9)]]
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        with patch('monitor_app.panda.queries.connections', {'panda': conn}), \
                patch('monitor_app.panda.queries._declared_failures', return_value=None):
            result = error_summary(status='failed')
        self.assertEqual(result['task_counts'], [{'taskid': 40856, 'count': 152}])
        self.assertEqual(result['status_counts'], {'failed': 152, 'closed': 9})
