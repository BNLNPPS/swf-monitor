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
