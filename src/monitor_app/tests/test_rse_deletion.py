"""Removing datasets from a disk RSE through the monitor
(monitor_app/panda/rse_deletions.py): the preview and confirmation
requests, the guards on confirmation, and the agent's callback. The
ActiveMQ send is mocked; no catalog is read."""
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient, APITestCase

from monitor_app.models import RseDeletion

SEND = 'monitor_app.panda.rse_deletions.ActiveMQConnectionManager'

PLAN = {'rse': 'BNL-XRD', 'pattern': '/RECO/26.03.0/*', 'datasets': [
    {'name': '/RECO/26.03.0/a', 'files': 10, 'bytes': 1000, 'eligible': True,
     'reason': '', 'tape': 'JLAB-TAPE-SE rule OK, 10/10 files'},
    {'name': '/RECO/26.03.0/b', 'files': 10, 'bytes': 1000, 'eligible': False,
     'reason': 'tape copy incomplete', 'tape': ''},
], 'totals': {'datasets': 2, 'eligible': 1, 'refused': 1,
              'eligible_files': 10, 'eligible_bytes': 1000}}


class RseDeletionApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username=f'op_{uuid.uuid4()}', password='x')
        self.client.force_authenticate(user=self.user)
        patcher = mock.patch(SEND)
        self.send = patcher.start().return_value.send_message
        self.send.return_value = True
        self.addCleanup(patcher.stop)

    def _previewed(self, **kw):
        fields = dict(rse='BNL-XRD', pattern='/RECO/26.03.0/*', status='previewed',
                      plan=PLAN, previewed_at=timezone.now())
        fields.update(kw)
        return RseDeletion.objects.create(**fields)

    def test_preview_queues_the_agent_and_refuses_bad_requests(self):
        url = reverse('monitor_app:panda-rse-deletion-request')
        r = self.client.post(url, {'rse': 'BNL-XRD', 'pattern': '/RECO/26.03.0/*'}, format='json')
        self.assertEqual(r.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(r.data['status'], 'previewing')
        self.assertIn('"phase": "preview"', self.send.call_args[0][1])
        for body in ({'rse': 'JLAB-TAPE-SE', 'pattern': '/RECO/26.03.0/*'},
                     {'rse': 'BNL-XRD', 'pattern': '/RECO/*'}):
            self.assertEqual(self.client.post(url, body, format='json').status_code, 400)

    def test_preview_needs_sign_in(self):
        self.client.force_authenticate(user=None)
        r = self.client.post(reverse('monitor_app:panda-rse-deletion-request'),
                             {'rse': 'BNL-XRD', 'pattern': '/RECO/26.03.0/*'}, format='json')
        self.assertIn(r.status_code, (401, 403))

    def test_execute_sends_only_eligible_names_after_typed_confirmation(self):
        rec = self._previewed()
        url = reverse('monitor_app:panda-rse-deletion-execute', args=[rec.id])
        self.assertEqual(self.client.post(url, {'confirm_rse': 'EIC-XRD'}, format='json').status_code, 400)
        r = self.client.post(url, {'confirm_rse': 'BNL-XRD'}, format='json')
        self.assertEqual(r.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(r.data['status'], 'queued')
        sent = self.send.call_args[0][1]
        self.assertIn('/RECO/26.03.0/a', sent)
        self.assertNotIn('/RECO/26.03.0/b', sent)
        # A second confirmation of the same request is refused.
        self.assertEqual(self.client.post(url, {'confirm_rse': 'BNL-XRD'}, format='json').status_code, 409)

    def test_execute_refuses_a_stale_preview(self):
        rec = self._previewed(previewed_at=timezone.now() - timedelta(hours=2))
        r = self.client.post(reverse('monitor_app:panda-rse-deletion-execute', args=[rec.id]),
                             {'confirm_rse': 'BNL-XRD'}, format='json')
        self.assertEqual(r.status_code, 409)
        self.send.assert_not_called()

    def test_one_removal_per_rse_at_a_time(self):
        self._previewed(status='running')
        rec = self._previewed()
        r = self.client.post(reverse('monitor_app:panda-rse-deletion-execute', args=[rec.id]),
                             {'confirm_rse': 'BNL-XRD'}, format='json')
        self.assertEqual(r.status_code, 409)

    def test_agent_callback_is_token_only_and_follows_the_lifecycle(self):
        rec = RseDeletion.objects.create(rse='BNL-XRD', pattern='/RECO/26.03.0/*')
        url = reverse('monitor_app:panda-rse-deletion-update', args=[rec.id])
        agent = APIClient()
        agent.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=self.user).key}')
        r = agent.post(url, {'status': 'previewed', 'plan': PLAN}, format='json')
        self.assertEqual(r.status_code, 200)
        rec.refresh_from_db()
        self.assertEqual((rec.status, rec.plan['totals']['eligible']), ('previewed', 1))
        self.assertIsNotNone(rec.previewed_at)
        # previewed cannot jump to done without a confirmation.
        self.assertEqual(agent.post(url, {'status': 'done'}, format='json').status_code, 409)
