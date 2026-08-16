from datetime import datetime, timezone
from unittest.mock import patch

from django.test import SimpleTestCase
from django.template.loader import render_to_string

from monitor_app.snapper_providers import (_epicprod_curve_values,
                                           _panda_card, _site_focus_view,
                                           _site_groups)


class EpicprodCurveValuesTests(SimpleTestCase):
    @patch('monitor_app.snapper_providers._panda_sites',
           return_value=('SITE_A',))
    def test_site_panels_are_stacked_with_cores_on_top(self, _sites):
        groups = {group['name']: group for group in _site_groups()}

        jobs = groups['Site jobs SITE_A']
        self.assertTrue(jobs['stacked'])
        self.assertEqual(jobs['order'][-2:],
                         ['sj_SITE_A_running', 'sjc_SITE_A'])
        self.assertEqual(jobs['default_off_ids'],
                         ['sj_SITE_A_activated'])
        for name in ('Site outcomes SITE_A', 'Site failures SITE_A',
                     'Site tasks SITE_A'):
            self.assertTrue(groups[name]['stacked'])

    @patch('monitor_app.snapper_providers._panda_sites',
           return_value=('SITE_A',))
    def test_site_focus_uses_a_panda_only_focus_series_product(self, _sites):
        focus = _site_focus_view()

        self.assertTrue(focus['cache_series'])
        self.assertEqual(focus['components'], ('panda',))
        self.assertFalse(focus['prewarm_series'])

    def test_sent_jobs_stay_in_state_but_not_plot_curves(self):
        state = {
            'components': {
                'panda': {
                    'data': {
                        'jobs': {
                            'in_flight_now': {
                                'running_cores': 16,
                                'by_status': {
                                    'activated': 100,
                                    'sent': 40,
                                    'running': 10,
                                },
                                'by_type': {'epicproduction': 150},
                                'by_type_status': {
                                    'epicproduction': {
                                        'activated': 100,
                                        'sent': 40,
                                        'running': 10,
                                    },
                                },
                            },
                            'sites': {
                                'SITE_A': {
                                    'by_status_now': {
                                        'activated': 100,
                                        'sent': 40,
                                        'running': 10,
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }

        values = _epicprod_curve_values(state)

        self.assertNotIn('job_sent', values)
        self.assertNotIn('ts_epicproduction_sent', values)
        self.assertNotIn('sj_SITE_A_sent', values)
        self.assertEqual(values['type_epicproduction'], 10)
        self.assertEqual(values['job_running'], 10)
        self.assertEqual(values['ts_epicproduction_running'], 10)
        self.assertEqual(values['job_activated'], 100)

    def test_scope_cut_is_six_vertical_plot_aligned_tables(self):
        data = {
            'jobs': {
                'cum': {'finished': 120, 'failed': 8},
                'in_flight_now': {
                    'by_status': {
                        'activated': 100, 'sent': 40, 'starting': 20,
                        'holding': 2, 'running': 10,
                    },
                    'by_type': {'epicproduction': 172},
                    'by_type_status': {
                        'epicproduction': {
                            'activated': 100, 'sent': 40, 'starting': 20,
                            'holding': 2, 'running': 10,
                        },
                    },
                },
                'sites': {},
            },
            'tasks': {
                'in_flight_now': {
                    'by_status': {'ready': 7, 'running': 3},
                },
                'sites': {},
            },
        }
        previous = {
            'jobs': {
                'cum': {'finished': 115, 'failed': 7},
                'in_flight_now': {
                    'by_status': {'holding': 1, 'running': 9},
                    'by_type_status': {
                        'epicproduction': {'holding': 1, 'running': 9},
                    },
                },
            },
            'tasks': {'in_flight_now': {'by_status': {'running': 2}}},
        }
        card = _panda_card(data, previous, {
            'params': {},
            'since': datetime(2026, 8, 1, tzinfo=timezone.utc),
            'since_data': {'jobs': {'cum': {'finished': 100, 'failed': 5}}},
        })
        card.update({
            'name': 'panda',
            'template': 'monitor_app/_snapper_cards.html',
            'payload_json': '{}',
        })

        self.assertTrue(card['split_panels'])
        self.assertEqual(card['types'][0]['value'], 12)
        self.assertEqual([row['label'] for row in card['states']],
                         ['holding', 'running'])
        self.assertEqual([row['label'] for row in card['tasks']],
                         ['running'])
        self.assertEqual([row['value'] for row in card['outcomes']], [20, 3])

        html = render_to_string(
            'snapper_ai/_snapper_cut.html',
            {
                'requested_at': None,
                'cards': [{
                    'name': 'health', 'kind': 'health',
                    'chip': {'color': '#2e7d32', 'value': 'ok'},
                    'counts': {'ok': 5}, 'non_ok_checks': [],
                    'payload_json': '{}',
                }, card],
            },
        )
        positions = [html.index(f'<strong>{title}</strong>') for title in (
            'Health', 'Queues', 'Outcomes', 'Types', 'States', 'Tasks')]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('d-flex flex-column gap-3 align-items-stretch', html)
        self.assertNotIn('Type × state', html)
        self.assertNotIn('job_sent', html)
