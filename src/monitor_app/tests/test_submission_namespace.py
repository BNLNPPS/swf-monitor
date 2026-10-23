"""Submission refuses occupied Rucio namespaces without modifying them."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from rucio.common.exception import DataIdentifierNotFound

SCRIPTS = Path(__file__).resolve().parents[3] / 'scripts'
spec = importlib.util.spec_from_file_location('submit_evgen_guard', SCRIPTS / 'submit-evgen-task.py')
submit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(submit)


class SubmissionNamespaceTest(unittest.TestCase):
    def test_jedi_suffixed_dataset_is_collision_without_template_container(self):
        client = MagicMock()
        client.get_did.side_effect = DataIdentifierNotFound()
        client.list_dids.return_value = iter(['group.EIC.test_log.123.123'])
        with patch('rucio.client.Client', return_value=client):
            with self.assertRaisesRegex(RuntimeError, 'test_log.123.123'):
                submit._require_fresh_bnl_namespace({'outDS': 'group.EIC.test'})
        self.assertEqual(client.list_dids.call_args.kwargs['filters'],
                         {'name': 'group.EIC.test_log.*'})
        client.add_dataset.assert_not_called()

    def test_orphan_log_file_is_collision(self):
        client = MagicMock()
        client.get_did.side_effect = DataIdentifierNotFound()
        client.list_dids.side_effect = [iter([]), iter(['group.EIC.test.000001.log.tgz'])]
        with patch('rucio.client.Client', return_value=client):
            with self.assertRaisesRegex(RuntimeError, 'log name already used'):
                submit._require_fresh_bnl_namespace({'outDS': 'group.EIC.test'})

    def test_no_log_and_no_stageout_needs_no_bnl_client(self):
        with patch('rucio.client.Client') as client:
            submit._require_fresh_bnl_namespace({'outDS': 'group.EIC.test', 'noLog': True})
        client.assert_not_called()

    def test_bnl_catalog_error_is_not_absence(self):
        client = MagicMock()
        client.get_did.side_effect = RuntimeError('catalog unavailable')
        with patch('rucio.client.Client', return_value=client):
            with self.assertRaisesRegex(RuntimeError, 'catalog unavailable'):
                submit._require_fresh_bnl_namespace({'outDS': 'group.EIC.test'})

    def precreate(self, client):
        args = SimpleNamespace(canary_stamp=None, trial=False, proxy='proxy')
        task = {'outDS': 'group.EIC.test', 'outputs': [
            {'dataset': '/fresh', 'level': 'FULL'}, {'dataset': '/second', 'level': 'RECO'}]}
        evgen = SimpleNamespace(RUCIO_SCOPE='epic', rucio_client=lambda proxy: client)
        with patch.object(submit, '_load_sibling', return_value=evgen), \
             patch.object(submit, '_require_fresh_bnl_namespace'):
            return submit._precreate_outputs(task, args)

    def test_second_jlab_collision_does_not_create_first(self):
        client = MagicMock()
        client.get_did.side_effect = [DataIdentifierNotFound(), {'type': 'DATASET'}]
        with self.assertRaisesRegex(RuntimeError, 'epic:/second'):
            self.precreate(client)
        client.add_dataset.assert_not_called()
        client.set_metadata.assert_not_called()
        client.add_replication_rule.assert_not_called()

    def test_jlab_error_does_not_create_anything(self):
        client = MagicMock()
        client.get_did.side_effect = RuntimeError('catalog unavailable')
        with self.assertRaisesRegex(RuntimeError, 'catalog unavailable'):
            self.precreate(client)
        client.add_dataset.assert_not_called()

    def test_fresh_outputs_created_only_after_all_lookups(self):
        client = MagicMock(account='eicprod')
        client.get_did.side_effect = DataIdentifierNotFound()
        result = self.precreate(client)
        calls = [call[0] for call in client.mock_calls]
        self.assertEqual(calls[:4], ['get_did', 'get_did', 'add_dataset', 'add_dataset'])
        self.assertEqual([row['dataset'] for row in result], ['epic:/fresh', 'epic:/second'])
        self.assertTrue(all(row['created'] for row in result))

    def test_stageout_names_follow_kernel_naming(self):
        names = submit._load_sibling('evgen_dataset_names.py').panda_dataset_names
        self.assertEqual(names({'outDS': 'group.EIC.test', 'stageOutFiles': ['report.json']}),
                         ['group.EIC.test_log/', 'group.EIC.test_report/'])
