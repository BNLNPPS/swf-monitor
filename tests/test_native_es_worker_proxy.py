import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dispatcher = load('es_dispatcher', 'evgen_job_dispatcher.py')
submitter = load('es_submitter', 'submit-evgen-task.py')


class NativeWorkerProxy(unittest.TestCase):
    def test_credential_appears_only_on_worker_and_canary_outputs_stay_expiring(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proxy = root / 'native-proxy'; proxy.write_text('PRIVATE_PROXY_FIXTURE')
            payload = root / 'payload'; (payload / 'es').mkdir(parents=True)
            (payload / 'VERSION').write_text('test')
            (payload / 'es/es_harness.py').write_text('import json,sys\nfrom pathlib import Path\n'
                'p=Path(sys.argv[sys.argv.index("--summary")+1]);p.parent.mkdir(parents=True,exist_ok=True)\n'
                'p.write_text(json.dumps({"args":sys.argv[1:]}))\n')
            spec = {'csvBase': 'native-test', 'csvRows': ['input,steer,20,0000'],
                    'env': {}, 'workerProxy': True, 'nEventsPerWorker': 5}
            with patch.object(submitter, '_reporting_env', return_value={}):
                work = Path(submitter._assemble_sandbox(spec, str(proxy), str(root / 'work'),
                                                        payload_dir=str(payload)))
            sandbox = work / 'sandbox'
            self.assertFalse(any(b'PRIVATE_PROXY_FIXTURE' in p.read_bytes()
                                 for p in sandbox.rglob('*') if p.is_file()))
            self.assertIn('X509_USER_PROXY=worker-proxy',
                          (sandbox / 'environment-native-test.sh').read_text())
            with patch.dict(os.environ, {'ES_WORKER_PROXY': '1', 'PILOT_X509_USER_PROXY': str(proxy),
                                          'ES_PAYLOAD_IMAGE': 'image', 'ES_SLOTS': '4'}, clear=True), \
                    patch.object(dispatcher, 'write_job_report'):
                rc = dispatcher.run_event_service('native-test', 'native-test', str(sandbox), 'channel')
            self.assertEqual(rc, 0)
            self.assertEqual((sandbox / 'worker-proxy').read_text(), 'PRIVATE_PROXY_FIXTURE')
            self.assertEqual((sandbox / 'worker-proxy').stat().st_mode & 0o777, 0o600)
            # The close starts with --cleanenv, then sources the environment
            # file: both credentials must resolve in that new process.
            check = subprocess.run(
                ['/bin/bash', '-c',
                 'source environment-native-test.sh\nexec "$1" -c "$2"',
                 'close-test', sys.executable,
                 'import os,json;print(json.dumps({k:os.path.realpath(os.environ[k]) '
                 'for k in ["X509_USER_PROXY","PILOT_X509_USER_PROXY"]}))'],
                cwd=sandbox, env={'PATH': os.environ['PATH']},
                capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(check.stdout), {
                'X509_USER_PROXY': str(sandbox / 'worker-proxy'),
                'PILOT_X509_USER_PROXY': str(sandbox / 'worker-proxy')})
            args = json.loads((work / 'es_work/es_summary.json').read_text())['args']
            self.assertIn('CANARY_OUTPUT_DATASET=TEST/canary/native-test', args)
            self.assertIn('CANARY_LIFETIME_S=604800', args)

    def test_missing_native_credential_refuses_without_a_fallback(self):
        with patch.dict(os.environ, {'ES_WORKER_PROXY': '1', 'ES_PAYLOAD_IMAGE': 'image'}, clear=True):
            self.assertEqual(dispatcher.run_event_service('test', 'test', '/not-used', 'channel'), 2)
