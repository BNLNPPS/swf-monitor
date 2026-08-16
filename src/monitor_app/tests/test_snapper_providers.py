from django.test import SimpleTestCase

from monitor_app.snapper_providers import _epicprod_curve_values


class EpicprodCurveValuesTests(SimpleTestCase):
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
