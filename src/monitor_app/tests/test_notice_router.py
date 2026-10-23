from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from monitor_app.notice_router import _compose, route_new_events
from monitor_app.live_notices import STATE_KEY as LIVE_STATE_KEY


class NoticeCompositionTests(SimpleTestCase):
    @patch('monitor_app.models.external_face_base_url',
           return_value='https://monitor.example')
    def test_operation_and_subject_label_replace_internal_batch_id(self, _base):
        row = SimpleNamespace(id=42, funcname='event')
        notice = _compose(row, {
            'action': 'panda_task_operation',
            'operation': 'resume',
            'subject_key': '03cae1f8-4fb2-4493-a80e-f1e67f1d985b',
            'subject_label': 'PanDA tasks 38941, 38942',
            'outcome': 'ok',
            'summary': '2/2 verified',
            'url': '/panda/tasks/',
        })

        self.assertEqual(notice['title'],
                         'resume: PanDA tasks 38941, 38942')
        self.assertEqual(notice['detail'], '2/2 verified')
        self.assertEqual(notice['url'],
                         'https://monitor.example/prod/panda/tasks/')


class LiveRoutingTests(SimpleTestCase):
    @patch('monitor_app.notice_router._init_high_water')
    @patch('monitor_app.epicprod_logging.get_live_policy', return_value={})
    @patch('monitor_app.models.external_face_base_url', return_value='https://monitor.example')
    @patch('monitor_app.models.PersistentState')
    @patch('monitor_app.models.CapcomNotice')
    @patch('monitor_app.models.NoticeSubscription')
    @patch('monitor_app.models.AppLog')
    @patch('monitor_app.notice_plugins.PLUGINS')
    def test_quiet_maintenance_still_buffers_and_recovery_is_published(
            self, plugins, logs, subscriptions, buffer, state, _base, _policy, _init):
        live = SimpleNamespace(subscriber='epicprod-live', event='*',
                               filters={'live': True, 'sublevel': ['high', 'normal']},
                               delivery='mattermost-live')
        other = SimpleNamespace(subscriber='automation', event='*', filters={}, delivery='buffer')
        subscriptions.objects.filter.return_value = [live, other]
        rows = [SimpleNamespace(
            id=i, app_name='epicprod', instance_name='ops-agent', funcname='catalog_sync',
            message='catalog sync', extra_data=dict(
                action='catalog_sync', outcome=outcome, username='nightly_cron',
                live_default=visible, sublevel='high' if visible else 'low', reason=reason))
            for i, outcome, visible, reason in (
                (1, 'ok', True, ''), (2, 'error', True, 'timeout'),
                (3, 'error', True, 'timeout'), (4, 'ok', False, ''))]
        logs.objects.filter.return_value.order_by.return_value.__getitem__.return_value = rows
        state.get_state.return_value = {'notice_router_last_id': 0}
        buffer.objects.get_or_create.return_value = (None, True)
        plugin = MagicMock()
        plugins.get.return_value = plugin

        self.assertEqual(route_new_events(), 6)

        self.assertEqual(buffer.objects.get_or_create.call_count, 4)
        self.assertEqual(plugin.deliver.call_count, 2)
        self.assertEqual(plugin.deliver.call_args_list[0].args[1]['outcome'], 'error')
        self.assertEqual(plugin.deliver.call_args_list[1].args[1]['operation'], 'catalog_sync_recovered')
        self.assertEqual(state.update_state.call_args.args[0],
                         {'notice_router_last_id': 4, LIVE_STATE_KEY: {}})
