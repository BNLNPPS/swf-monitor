#!/usr/bin/env python3
"""
Client-API EVGEN submission kernel — our owned reproduction of the proven
condor-side recipe (eic/job_submission_condor submit_panda_api.py, spec only).

Given a PCS-built spec (``--spec spec.json``) and an already-assembled sandbox
directory (``--workdir``), this builds the PanDA taskParamMap, uploads the
sandbox to the PanDA cache, and submits the task with pandaclient under the
caller's PanDA identity. It is run by scripts/submit-evgen-task.py inside a
shell that has sourced the panda-client environment (so ``pandaclient`` and a
valid OIDC token are present); it is not the credentialed orchestrator itself.

The taskParamMap is noInput+noOutput: the containerized payload xrootd-streams
the EVGEN input from JLab and self-registers RECO to JLab Rucio, so PanDA stays
out of the science-data path (docs/JEDI_INTEGRATION.md § single-Rucio
constraint). The %RNDM=0 in the exec becomes a per-job ${SEQNUMBER} that selects
the manifest row.

On success it prints a normalized ``jediTaskID=<N>`` line the orchestrator
parses. Every failure path is surfaced (stderr + non-zero exit); nothing is
swallowed.
"""
import argparse
import json
import os
import re
import sys
import tarfile
import tempfile
import uuid

from pandaclient import panda_api
from pandaclient import Client

# The generic-payload TRF that runGen wraps; same value the proven recipe uses.
TRANS_PATH = "https://pandaserver-doma.cern.ch/trf/user/runGen-00-00-02"


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _source_url():
    """sourceURL for ${SURL} substitution, from the client's SSL base URL —
    the same extraction the proven recipe does (matches prun)."""
    base = getattr(Client, 'baseURLSSL', '')
    m = re.search(r'(https?://[^/]+)/', base)
    return m.group(1) if m else None


def _upload_sandbox(workdir):
    """Tar every file in the sandbox dir and upload it to the PanDA cache,
    returning the (possibly de-duplicated) archive name. Mirrors prun --noBuild
    / submit_panda_api.py behaviour."""
    archive_name = f'jobO.{uuid.uuid4().hex}.tar.gz'
    with tempfile.TemporaryDirectory() as tmpdir:
        archive_path = os.path.join(tmpdir, archive_name)
        _log(f"packing sandbox {workdir} -> {archive_name}")
        with tarfile.open(archive_path, 'w:gz') as tar:
            for fname in sorted(os.listdir(workdir)):
                fpath = os.path.join(workdir, fname)
                if os.path.isfile(fpath):
                    tar.add(fpath, arcname=fname)   # flat: name only, no path
                    _log(f"  + {fname}")
        old_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            status, out = Client.putFile(archive_name, False,
                                         useCacheSrv=False, reuseSandbox=True)
        finally:
            os.chdir(old_cwd)
    if out.startswith("NewFileName:"):
        archive_name = out.split(":")[-1]
        _log(f"reusing existing sandbox: {archive_name}")
    elif out != "True":
        _log(f"sandbox upload output: {out}")
        if status != 0:
            raise RuntimeError(f"sandbox upload failed (status {status})")
    else:
        _log(f"uploaded sandbox {archive_name}")
    return archive_name


def build_task_params(spec, archive_name):
    """Assemble the taskParamMap from the PCS spec and the uploaded sandbox."""
    params = {
        'vo': spec.get('vo', 'epic'),
        'site': spec.get('site', 'BNL_OSG_PanDA_1'),
        'workingGroup': spec.get('workingGroup', 'EIC'),
        'prodSourceLabel': spec.get('prodSourceLabel', 'test'),
        'processingType': spec.get('processingType', 'epicproduction'),
        'taskType': spec.get('taskType', 'prod'),
        # Producer envelope from commands.py (the proven 36439 path). A prod-role
        # submission gets NO server-side defaults — insertTaskParamsPanda only
        # fills userName/taskType/taskPriority on the non-prodRole branch — so a
        # producer must supply them itself. The omitted taskPriority is precisely
        # what broke this Sakib-derived user-mode template under our prod token.
        'taskPriority': int(spec.get('taskPriority', 900)),
        'cloud': spec.get('cloud', spec.get('workingGroup', 'EIC')),
        'campaign': spec.get('campaign', ''),
        'taskName': spec['outDS'],
        'userName': spec.get('userName') or os.environ.get('SWF_TASK_OWNER') or None,
        'noInput': True,                 # payload stages its own EVGEN input
        'noOutput': True,                # payload self-registers RECO to JLab
        'architecture': '',
        'transUses': '',
        'transHome': None,
        'transPath': TRANS_PATH,
        'sourceURL': _source_url(),
        'coreCount': int(spec.get('nCore', 1)),
        'ramCount': int(spec.get('memory', 4096)),
        'ramUnit': 'MBPerCore',                      # producer envelope (commands.py)
        'nEvents': int(spec.get('nJobs', 1)),       # one job per manifest row
        'nEventsPerJob': int(spec.get('nEventsPerJob', 1)),
        'jobParameters': [
            {'type': 'constant', 'value': '-j "" --sourceURL ${SURL}'},
            {'type': 'constant', 'value': '-r .'},
        ],
        'multiStepExec': {
            'preprocess': {'command': '${TRF}', 'args': '--preprocess ${TRF_ARGS}'},
            'postprocess': {'command': '${TRF}', 'args': '--postprocess ${TRF_ARGS}'},
            'containerOptions': {
                'containerExec': ('echo "=== cat exec script ==="; cat __run_main_exec.sh; '
                                  'echo; echo "=== exec script ==="; /bin/sh __run_main_exec.sh'),
                'containerImage': spec.get('containerImage', ''),
            },
        },
        'log': {
            'type': 'template',
            'param_type': 'log',
            'value': f"{spec['outDS']}.$JEDITASKID.${{SN}}.log.tgz",
            'dataset': spec['outDS'] + '_log/',
            'hidden': True,
        },
    }

    disk = spec.get('disk')
    if disk is not None:
        params['workDiskCount'] = int(disk)
        params['workDiskUnit'] = 'MB'

    # Scouts off -> walltime used directly; scouts on -> HS06 per-event routing
    # (avoids the noInput pseudo-input 1MB-file walltime inflation).
    if spec.get('skipScout'):
        params['skipScout'] = True
    else:
        params['cpuTimeUnit'] = 'HS06sPerEvent'
    params['walltime'] = int(float(spec.get('walltimeHours', 2.0)) * 3600)

    if spec.get('containerImage'):
        params['container_name'] = spec['containerImage']

    # -a <sandbox>
    params['jobParameters'].append({'type': 'constant', 'value': f'-a {archive_name}'})

    # %RNDM=X -> ${SEQNUMBER}; add the pseudo_input template (one job per row).
    exec_cmd = spec['exec']
    rndm = re.search(r'%RNDM(:|=)(\d+)', exec_cmd)
    if rndm:
        offset = rndm.group(2)
        exec_cmd = re.sub(r'%RNDM(:|=)\d+', '${SEQNUMBER}', exec_cmd)
        params['jobParameters'].append({
            'type': 'template', 'param_type': 'pseudo_input',
            'value': '${SEQNUMBER}', 'dataset': 'seq_number',
            'offset': offset, 'hidden': True,
        })

    # -p "<url-encoded exec>"
    encoded = exec_cmd.replace(' ', '%20')
    params['jobParameters'].extend([
        {'type': 'constant', 'value': '-p "', 'padding': False},
        {'type': 'constant', 'value': encoded},
        {'type': 'constant', 'value': '"'},
    ])
    return params


def main():
    ap = argparse.ArgumentParser(description="Submit a client-API EVGEN task to PanDA.")
    ap.add_argument("--spec", required=True, help="PCS-built spec JSON file")
    ap.add_argument("--workdir", default="sandbox",
                    help="assembled sandbox dir to tar and upload")
    args = ap.parse_args()

    with open(args.spec) as f:
        spec = json.load(f)
    if not spec.get('outDS') or not spec.get('exec'):
        _log("ERROR: spec missing outDS/exec")
        return 2
    if not os.path.isdir(args.workdir):
        _log(f"ERROR: sandbox dir not found: {args.workdir}")
        return 2

    try:
        archive_name = _upload_sandbox(args.workdir)
    except Exception as e:
        _log(f"ERROR: sandbox upload failed: {e}")
        return 3

    params = build_task_params(spec, archive_name)
    if not params.get('sourceURL'):
        _log("ERROR: could not derive sourceURL from Client.baseURLSSL "
             "(no ${SURL} for the payload) — refusing to submit")
        return 3
    if not params.get('container_name'):
        _log("ERROR: no container image in spec — refusing to submit")
        return 3
    client = panda_api.get_api()
    result = client.submit_task(params)
    _log(f"submit_task result: {result}")

    m = re.search(r'jediTaskID[=:\s]+(\d+)', str(result))
    ok = bool(result) and result[0] == 0
    if m:
        # Normalized line the orchestrator parses.
        print(f"jediTaskID={m.group(1)}")
    if ok and m:
        return 0
    _log("ERROR: submission did not return a jediTaskID / non-zero status")
    return 1


if __name__ == "__main__":
    sys.exit(main())
