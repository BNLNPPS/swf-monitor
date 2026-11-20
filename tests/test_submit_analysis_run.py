import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = 'ab' * 32


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


doer = load('analysis_submitter', 'submit-analysis-run.py')

# The kernel imports pandaclient at load; the parameter build needs none of it.
sys.path.insert(0, str(ROOT / 'scripts'))
pandaclient = types.ModuleType('pandaclient')
pandaclient.panda_api = types.ModuleType('pandaclient.panda_api')
pandaclient.Client = types.SimpleNamespace(baseURLSSL='https://pandaserver.example:25443/server/panda')
with patch.dict(sys.modules, {'pandaclient': pandaclient,
                              'pandaclient.panda_api': pandaclient.panda_api}):
    kernel = load('analysis_kernel', 'evgen_panda_submit.py')


def map_spec(**kw):
    """A map specification in the shape swf-epicprod analysis.services builds."""
    base = f'analysis_{RUN_ID[:12]}_map'
    spec = {
        'kind': 'analysis', 'stage': 'map', 'runCardId': RUN_ID,
        'outDS': f'group.EIC.analysis.a1.{RUN_ID[:12]}.map',
        'vo': 'epic', 'userName': 'rahmans', 'workingGroup': 'EIC', 'site': 'BNL_PanDA_1',
        'prodSourceLabel': 'user', 'taskType': 'anal', 'processingType': 'epicanalysis',
        'gshare': 'User', 'containerImage': '/cvmfs/example/eic_xl:26.07.3-stable',
        'nCore': 1, 'memory': 2000, 'walltimeHours': 4.0, 'skipScout': True,
        'disableReassign': True, 'nEventsPerJob': 1, 'nJobs': 2,
        'exec': f'bash payload/analysis_run.sh %RNDM=0 {base}',
        'csvBase': base, 'csvRows': ['0;ep;0000;/RECO/a.root', '1;ep;0001;/RECO/b.root'],
        'env': {'ANALYSIS_STAGE': 'map', 'ANALYSIS_RUN_ID': RUN_ID,
                'ANALYSIS_PARAMS': 'DIHADRON_TRIG_LOW=2 MODE=reco'},
    }
    spec.update(kw)
    return spec


def fake_payload(root):
    payload = Path(root) / 'payload'
    payload.mkdir()
    (payload / 'VERSION').write_text('v-test\n')
    (payload / 'analysis_run.sh').write_text('#!/bin/bash\n')
    (payload / 'djangoh-4.6.21.tgz').write_text('')
    (payload / 'rucio-clients-40.2.0.tgz').write_text('')
    (payload / '__pycache__').mkdir()
    (payload / '__pycache__' / 'x.pyc').write_text('')
    return str(payload)


class Sandbox(unittest.TestCase):

    def test_map_sandbox_carries_manifest_environment_proxy_and_payload(self):
        with tempfile.TemporaryDirectory() as d:
            proxy = Path(d) / 'proxy'
            proxy.write_text('PROXY_FIXTURE')
            spec = map_spec()
            work = Path(doer.assemble_sandbox(spec, str(proxy), str(Path(d) / 'w'),
                                              payload_dir=fake_payload(d),
                                              reporting={'REPORT_OUT_BUCKET': 'reports'}))
            sandbox = work / 'sandbox'
            base = spec['csvBase']
            self.assertEqual((sandbox / f'{base}.csv').read_text().splitlines(), spec['csvRows'])
            self.assertEqual((sandbox / doer.PROXY_SANDBOX_NAME).read_text(), 'PROXY_FIXTURE')
            self.assertEqual(stat.S_IMODE((sandbox / doer.PROXY_SANDBOX_NAME).stat().st_mode), 0o600)
            self.assertTrue((sandbox / 'payload' / 'analysis_run.sh').is_file())
            self.assertFalse((sandbox / 'payload' / '__pycache__').exists())
            self.assertFalse((sandbox / 'payload' / 'djangoh-4.6.21.tgz').exists())
            self.assertTrue((sandbox / 'payload' / 'rucio-clients-40.2.0.tgz').exists())
            self.assertFalse((sandbox / 'spec.json').exists())
            self.assertEqual(json.loads((work / 'spec.json').read_text())['payload_version'], 'v-test')
            # The job sources the file in its own directory: values with
            # spaces survive and the proxy resolves there.
            env_file = sandbox / f'environment-{base}.sh'
            out = subprocess.run(
                ['bash', '-c', f'source {env_file}; printf "%s|%s|%s" '
                 '"$ANALYSIS_PARAMS" "$X509_USER_PROXY" "$REPORT_OUT_BUCKET"'],
                cwd=sandbox, capture_output=True, text=True, check=True).stdout
            self.assertEqual(out, f'DIHADRON_TRIG_LOW=2 MODE=reco|{sandbox}/'
                                  f'{doer.PROXY_SANDBOX_NAME}|reports')

    def test_merge_sandbox_writes_the_runs_files(self):
        with tempfile.TemporaryDirectory() as d:
            spec = map_spec(stage='merge', csvBase=f'analysis_{RUN_ID[:12]}_merge',
                            sandboxFiles={'analysis_run_card.json': '{"a": 1}',
                                          'analysis_jobs.json': '[]'})
            work = Path(doer.assemble_sandbox(spec, '', str(Path(d) / 'w'),
                                              payload_dir=fake_payload(d)))
            sandbox = work / 'sandbox'
            self.assertEqual((sandbox / 'analysis_run_card.json').read_text(), '{"a": 1}')
            self.assertFalse((sandbox / doer.PROXY_SANDBOX_NAME).exists())
            self.assertNotIn('X509_USER_PROXY',
                             (sandbox / f"environment-{spec['csvBase']}.sh").read_text())

    def test_unsafe_file_name_and_missing_payload_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            payload = fake_payload(d)
            with self.assertRaises(ValueError):
                doer.assemble_sandbox(map_spec(sandboxFiles={'../x': ''}), '',
                                      str(Path(d) / 'w'), payload_dir=payload)
            os.remove(os.path.join(payload, 'analysis_run.sh'))
            with self.assertRaises(RuntimeError):
                doer.assemble_sandbox(map_spec(), '', str(Path(d) / 'w'), payload_dir=payload)


class KernelParams(unittest.TestCase):

    def test_analysis_spec_becomes_a_user_task_on_its_share(self):
        params = kernel.build_task_params(map_spec(), 'sandbox.tgz')
        self.assertEqual(params['taskName'], f'group.EIC.analysis.a1.{RUN_ID[:12]}.map')
        self.assertEqual((params['prodSourceLabel'], params['taskType'], params['gshare']),
                         ('user', 'anal', 'User'))
        self.assertEqual((params['ramCount'], params['nEvents'], params['coreCount']), (2000, 2, 1))
        self.assertTrue(params['noInput'] and params['noOutput'] and params['skipScout'])
        seq = [p for p in params['jobParameters'] if p.get('dataset') == 'seq_number']
        self.assertEqual(seq[0]['offset'], '0')
        exec_value = ''.join(p['value'] for p in params['jobParameters']
                             if p.get('type') == 'constant')
        self.assertIn('payload/analysis_run.sh%20${SEQNUMBER}', exec_value)

    def test_a_spec_without_gshare_keeps_the_servers(self):
        spec = map_spec()
        del spec['gshare']
        self.assertNotIn('gshare', kernel.build_task_params(spec, 'sandbox.tgz'))


class Flow(unittest.TestCase):

    def run_doer(self, argv, kernel_result, posts=None, spec=None):
        """main() with PCS, the destination check and the kernel stubbed."""
        posts = posts if posts is not None else []
        spec = spec or map_spec()

        def get(base, path, query, token):
            self.assertEqual(path, f'/analysis/api/v1/runs/{RUN_ID}/spec/')
            self.assertEqual(query, {'stage': 'map'})
            return json.dumps(spec)

        def post(base, path, query, body, token, owner=None):
            posts.append((path, body, owner))
            if kernel_result[2:] == ('record-fails',):
                raise OSError('monitor down')
            return '{}'

        rc, out = kernel_result[:2]
        regulation = types.ModuleType('swf_epicprod.regulation')
        regulation.require_task_destination = lambda site: None
        with tempfile.TemporaryDirectory() as d, \
                patch.object(doer.evgen, '_api_get', get), \
                patch.object(doer.evgen, '_api_post_json', post), \
                patch.object(doer.evgen, '_payload_dir', return_value=fake_payload(d)), \
                patch.object(doer.evgen, '_reporting_env', return_value={}), \
                patch.object(doer, 'SUBMIT_TMP_ROOT', str(Path(d) / 'w')), \
                patch.object(doer.time, 'sleep'), \
                patch.dict(sys.modules, {'swf_epicprod': types.ModuleType('swf_epicprod'),
                                         'swf_epicprod.regulation': regulation}), \
                patch.object(doer.subprocess, 'run',
                             return_value=subprocess.CompletedProcess([], rc, out, '')) as run, \
                patch('sys.stdout', new_callable=io.StringIO) as stdout:
            proxy = Path(d) / 'proxy'
            proxy.write_text('P')
            code = doer.main(['--run-card-id', RUN_ID, '--stage', 'map', '--proxy', str(proxy),
                              '--swf-monitor-url', 'https://monitor.example/swf-monitor',
                              '--owner', 'rahmans'] + argv)
            spec_sent = None
            if run.called:
                spec_sent = json.loads((Path(run.call_args.kwargs['cwd']) / 'spec.json').read_text())
        return code, stdout.getvalue().strip(), posts, spec_sent

    def test_submitted_task_is_recorded_on_the_run(self):
        code, out, posts, sent = self.run_doer(['--label', 'test'], (0, 'jediTaskID=42200\n'))
        self.assertEqual((code, out), (0, '42200'))
        self.assertEqual(sent['prodSourceLabel'], 'test')
        path, body, owner = posts[0]
        self.assertEqual(path, f'/analysis/api/v1/runs/{RUN_ID}/record-submission/')
        self.assertEqual(body, {'stage': 'map', 'jedi_task_id': 42200,
                                'panda_task_name': f'group.EIC.analysis.a1.{RUN_ID[:12]}.map',
                                'payload_version': 'v-test'})
        self.assertEqual(owner, 'rahmans')

    def test_without_a_label_the_specs_is_sent(self):
        _, _, _, sent = self.run_doer([], (0, 'jediTaskID=1\n'))
        self.assertEqual(sent['prodSourceLabel'], 'user')

    def test_unrecorded_submission_exits_7_after_retries(self):
        posts = []
        code, out, posts, _ = self.run_doer([], (0, 'jediTaskID=42201\n', 'record-fails'), posts)
        self.assertEqual((code, out, len(posts)), (7, '42201', doer.RECORD_ATTEMPTS))

    def test_kernel_failure_and_missing_id(self):
        self.assertEqual(self.run_doer([], (1, 'boom'))[0], 5)
        self.assertEqual(self.run_doer([], (0, 'no id here'))[0], 6)

    def test_a_merge_not_ready_is_refused_before_anything_is_built(self):
        def refuse(*a, **k):
            raise urllib.error.HTTPError('u', 409, 'Conflict', {},
                                         io.BytesIO(b'{"detail": "2 of 3 map jobs"}'))
        with patch.object(doer.evgen, '_api_get', refuse), \
                patch.object(doer, 'assemble_sandbox') as build:
            code = doer.main(['--run-card-id', RUN_ID, '--stage', 'merge',
                              '--swf-monitor-url', 'https://monitor.example'])
        self.assertEqual(code, 2)
        build.assert_not_called()

    def test_a_spec_for_another_run_is_refused(self):
        with patch.object(doer.evgen, '_api_get',
                          return_value=json.dumps(map_spec(runCardId='cd' * 32))):
            code = doer.main(['--run-card-id', RUN_ID, '--stage', 'map',
                              '--swf-monitor-url', 'https://monitor.example'])
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()
