"""Exercise both dispatcher routes through a real child harness process."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
module_spec = importlib.util.spec_from_file_location('es_dispatcher', ROOT / 'scripts/evgen_job_dispatcher.py')
dispatcher = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(dispatcher)
submit_spec = importlib.util.spec_from_file_location('es_submitter', ROOT / 'scripts/submit-evgen-task.py')
submitter = importlib.util.module_from_spec(submit_spec)
submit_spec.loader.exec_module(submitter)


class ProductionDispatcher(unittest.TestCase):
    def run_harness(self, production):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory) / 'sandbox'; sandbox.mkdir()
            harness = sandbox / dispatcher.PAYLOAD_SUBDIR / 'es/es_harness.py'
            harness.parent.mkdir(parents=True)
            harness.write_text('import json,sys\nfrom pathlib import Path\n'
                               'p=Path(sys.argv[sys.argv.index("--summary")+1])\n'
                               'p.parent.mkdir(parents=True,exist_ok=True)\n'
                               'p.write_text(json.dumps({"args":sys.argv[1:]}))\n')
            proxy = Path(directory) / 'native-proxy'; proxy.write_text('fixture proxy')
            env = dict(os.environ, ES_PAYLOAD_IMAGE='image:26.07.3', ES_SLOTS='4',
                       PILOT_X509_USER_PROXY=str(proxy),
                       ES_OUTPUT_DATASET='/RECO/26.07.3/sample/10kevents')
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(dispatcher, 'write_job_report') as report:
                rc = dispatcher.run_event_service('sample', 'whole-task', str(sandbox),
                                                  'test-channel', production=production)
                self.assertEqual(rc, 0)
                if production:
                    self.assertEqual((sandbox / 'worker-proxy').read_text(), 'fixture proxy')
                    self.assertEqual((sandbox / 'worker-proxy').stat().st_mode & 0o777, 0o600)
                summary = json.loads((sandbox.parent / dispatcher.ES_WORK_DIR / 'es_summary.json').read_text())
                return summary['args'], report.call_args.kwargs['extra']['es']

    def test_production_keeps_normal_outputs_without_canary_lifetime(self):
        args, report = self.run_harness(True)
        self.assertFalse(any('CANARY_' in arg for arg in args))
        self.assertEqual(report['dataset'], 'epic:/RECO/26.07.3/sample/10kevents')

    def test_canary_keeps_its_existing_expiring_output_route(self):
        args, report = self.run_harness(False)
        self.assertIn('CANARY_OUTPUT_DATASET=TEST/canary/whole-task', args)
        self.assertIn('CANARY_LIFETIME_S=604800', args)
        self.assertEqual(report['dataset'], 'epic:/TEST/canary/whole-task')

    def test_production_sandbox_never_contains_the_live_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proxy = root / 'live-proxy'; proxy.write_text('PRIVATE_PROXY_FIXTURE')
            payload = root / 'payload-source'; payload.mkdir()
            (payload / 'VERSION').write_text('test')
            spec = {'csvBase': 'whole-sample', 'csvRows': ['input,hepmc3.tree.root,10000,0000'],
                    'env': {}, 'workerProxy': True, 'nEventsPerWorker': 250}
            with patch.object(submitter, '_reporting_env', return_value={}):
                work = Path(submitter._assemble_sandbox(spec, str(proxy), str(root / 'work'),
                                                        payload_dir=str(payload)))
            sandbox = work / 'sandbox'
            self.assertIn('X509_USER_PROXY=worker-proxy',
                          (sandbox / 'environment-whole-sample.sh').read_text())
            self.assertFalse((sandbox / proxy.name).exists())
            self.assertFalse(any(b'PRIVATE_PROXY_FIXTURE' in p.read_bytes()
                                 for p in sandbox.rglob('*') if p.is_file()))
