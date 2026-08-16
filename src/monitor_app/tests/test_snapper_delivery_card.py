from datetime import datetime, timezone
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from monitor_app.snapper_providers import _delivery_card


class DeliveryCardTests(SimpleTestCase):
    @patch('monitor_app.snapper_providers._pc_cache')
    def test_scope_cut_omits_campaign_detail(self, pc_cache):
        self.assertIsNone(_delivery_card(
            {'campaigns': {'26.08': {}}}, {}, {'params': {}}))
        pc_cache.assert_not_called()

    @patch('monitor_app.snapper_providers._pc_cache')
    def test_day_table_shows_event_completion_as_rightmost_column(
            self, pc_cache):
        pc_cache.return_value = {
            'requestors': {'pc1': ['PWG']},
            'keys': {'pc1': 'configuration'},
            'categories': {'pc1': 'DIS'},
        }
        data = {
            'campaigns': {
                '26.08': {
                    'totals': {
                        'arrived_files': 1,
                        'arrived_events': 10,
                        'events': 25,
                    },
                    'leaves': {
                        'pc1': {
                            'arrived_files': 1,
                            'arrived_events': 10,
                            'events': 25,
                            'cum_files': 2,
                            'expected': 100,
                            'tier': 'included',
                        },
                    },
                },
            },
        }

        card = _delivery_card(
            data,
            {},
            {
                'params': {'campaign': '26.08', 'lens': 'category'},
                'requested_at': datetime(2026, 8, 11, tzinfo=timezone.utc),
            },
        )

        row = card['campaigns'][0]['day_groups'][0]['rows'][0]
        self.assertEqual(row['completion'], 25.0)
        html = render_to_string(
            'monitor_app/_snapper_cards.html', {'card': card})
        self.assertIn('25.0%', html)
        self.assertLess(
            html.index('<th>Target events</th>'),
            html.index('<th>% complete</th>'),
        )
