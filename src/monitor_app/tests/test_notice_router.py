from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from monitor_app.notice_router import _compose


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
