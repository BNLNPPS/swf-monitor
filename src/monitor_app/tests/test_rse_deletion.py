"""Removing datasets from a disk RSE through the monitor
(monitor_app/panda/rse_deletions.py): the preview and confirmation
requests, the guards on confirmation, the operations-role gate, and the
reading of the agent's request files. No table and no database: the
ActiveMQ send and the authority lookup are mocked, and the request files
live in a temporary SWF_TMP_DIR."""
import json
import shutil
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from monitor_app.panda import api as panda_api
from monitor_app.panda import rse_deletions as rd

SEND = 'monitor_app.panda.rse_deletions.ActiveMQConnectionManager'

PLAN = {'rse': 'BNL-XRD', 'pattern': '/RECO/26.03.0/*', 'datasets': [
    {'name': '/RECO/26.03.0/a', 'files': 10, 'bytes': 1000, 'eligible': True,
     'reason': '', 'tape': 'JLAB-TAPE-SE rule OK, 10/10 files'},
    {'name': '/RECO/26.03.0/b', 'files': 10, 'bytes': 1000, 'eligible': False,
     'reason': 'tape copy incomplete', 'tape': ''},
], 'totals': {'datasets': 2, 'eligible': 1, 'refused': 1,
              'eligible_files': 10, 'eligible_bytes': 1000}}


class RseDeletionTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        settings_patch = override_settings(SWF_TMP_DIR=self.tmp)
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        patcher = mock.patch(SEND)
        self.send = patcher.start().return_value.send_message
        self.send.return_value = True
        self.addCleanup(patcher.stop)

    def _write(self, **fields):
        now = timezone.now()
        record = dict(id=str(uuid.uuid4()), rse='BNL-XRD', pattern='/RECO/26.03.0/*',
                      requested_by='op', status='previewed', plan=PLAN,
                      previewed_at=now.isoformat(), updated_at=now.isoformat())
        record.update(fields)
        directory = Path(self.tmp) / 'rse-deletions'
        directory.mkdir(exist_ok=True)
        (directory / f"{record['id']}.json").write_text(json.dumps(record))
        return record

    def _sent(self):
        return json.loads(self.send.call_args[0][1])

    def test_preview_queues_the_agent_and_refuses_bad_requests(self):
        rec = rd.request_preview(rse='BNL-XRD', pattern='/RECO/26.03.0/*', requested_by='op')
        self.assertEqual(rec['status'], 'previewing')
        msg = self._sent()
        self.assertEqual((msg['phase'], msg['deletion_id'], msg['rse']),
                         ('preview', rec['id'], 'BNL-XRD'))
        for rse, pattern in (('JLAB-TAPE-SE', '/RECO/26.03.0/*'), ('BNL-XRD', '/RECO/*')):
            with self.assertRaises(rd.RseDeletionRequestError):
                rd.request_preview(rse=rse, pattern=pattern, requested_by='op')

    def test_execute_sends_only_eligible_names_after_typed_confirmation(self):
        rec = self._write()
        with self.assertRaises(rd.RseDeletionRequestError):
            rd.request_execute(rec['id'], confirm_rse='EIC-XRD', confirmed_by='op')
        self.send.reset_mock()
        out = rd.request_execute(rec['id'], confirm_rse='BNL-XRD', confirmed_by='op')
        self.assertEqual(out['status'], 'queued')
        msg = self._sent()
        self.assertEqual((msg['phase'], msg['names'], msg['created_by']),
                         ('execute', ['/RECO/26.03.0/a'], 'op'))

    def test_execute_refuses_a_stale_or_unpreviewed_request(self):
        old = (timezone.now() - timedelta(hours=2)).isoformat()
        for rec in (self._write(previewed_at=old),
                    self._write(status='done'),
                    self._write(plan=dict(PLAN, datasets=PLAN['datasets'][1:]))):
            with self.assertRaises(rd.RseDeletionRequestError):
                rd.request_execute(rec['id'], confirm_rse='BNL-XRD', confirmed_by='op')
        with self.assertRaises(rd.RseDeletionRequestError) as e:
            rd.request_execute(str(uuid.uuid4()), confirm_rse='BNL-XRD', confirmed_by='op')
        self.assertEqual(e.exception.status, 404)
        self.send.assert_not_called()

    def test_a_pending_request_the_agent_abandoned_reads_failed(self):
        old = (timezone.now() - timedelta(hours=3)).isoformat()
        rec = rd.serialize(self._write(status='running', updated_at=old,
                                       result={'datasets': [{'name': 'x', 'outcome': 'removed'}]}))
        self.assertEqual(rec['status'], 'failed')
        self.assertFalse(rec['pending'])
        self.assertEqual(rec['result']['datasets'][0]['outcome'], 'removed')
        self.assertEqual(rd.serialize(self._write(status='running'))['status'], 'running')

    def test_unreadable_ids_are_not_found(self):
        self.assertIsNone(rd.read_request('../../etc/passwd'))
        self.assertIsNone(rd.read_request(str(uuid.uuid4())))

    def test_only_the_operations_role_acts_on_the_internal_monitor(self):
        factory = APIRequestFactory()
        body = {'rse': 'BNL-XRD', 'pattern': '/RECO/26.03.0/*'}
        for ops, tunnel, expected in ((False, False, 403), (True, True, 403), (True, False, 202)):
            request = factory.post('/api/panda/rse-deletions/', body, format='json')
            force_authenticate(request, user=User(username='op'))
            with mock.patch('monitor_app.authority.get_authority', return_value={}), \
                    mock.patch('monitor_app.authority.is_ops', return_value=ops), \
                    mock.patch.object(panda_api, 'is_tunnel_request', return_value=tunnel):
                response = panda_api.rse_deletion_request(request)
            self.assertEqual(response.status_code, expected, (ops, tunnel))
