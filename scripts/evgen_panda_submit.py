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
            # Every file under the sandbox with its path relative to the
            # sandbox root: a flat sandbox (the production CSV and runner)
            # packs exactly as before, and a sandbox carrying a package
            # tree (the canary kit) keeps it, so the job can import it.
            for root, dirs, files in os.walk(workdir):
                dirs[:] = sorted(d for d in dirs if d != '__pycache__')
                for fname in sorted(files):
                    fpath = os.path.join(root, fname)
                    arcname = os.path.relpath(fpath, workdir)
                    tar.add(fpath, arcname=arcname)
                    _log(f"  + {arcname}")
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
        # Fixed: JEDI's watchdog scout-data pass resets a MBPerCore task's
        # ramCount from its first finished jobs' PSS (75th percentile plus
        # 10 percent), and the job request is 0.9 of that; task 39951's
        # retries went out asking 2,556 MB against a 2,790 MB RSS footprint
        # and died where the glidein polices request_memory (segfault
        # finding f-12). MBPerCoreFixed keeps the submitted memory through
        # the run; a job's own memory retry still climbs.
        'ramUnit': 'MBPerCoreFixed',
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
            # ${SN} only. JEDI substitutes $JEDITASKID here for a task carrying
            # the test label, and not for a production one: job 2721298 staged
            # out against the literal name and died on pilot error 1165 with
            # its physics already made and registered, while the canary an hour
            # earlier got its task id substituted from this same template. The
            # log dataset is per-task, so the serial alone names the file.
            'value': f"{spec['outDS']}.${{SN}}.log.tgz",
            'dataset': spec['outDS'] + '_log/',
            'hidden': True,
        },
    }

    disk = spec.get('disk')
    if disk is not None:
        params['workDiskCount'] = int(disk)
        params['workDiskUnit'] = 'MB'

    # Event Service: presence of nEventsPerWorker switches the task to
    # ES mode in JEDI (TaskRefinerBase, inherited by GenTaskRefiner) —
    # events are split into ranges of this size for range-level
    # dispatch and bookkeeping.
    if spec.get('nEventsPerWorker'):
        params['nEventsPerWorker'] = int(spec['nEventsPerWorker'])
    # Fine-grained processing: the server's Event Service flavor without
    # the merge step (JEDI TaskRefinerBase fineGrainedProc; job flag 6).
    # Every event of the input is a range; a job's finished ranges count
    # at its end and the rest go back to the file for the next job
    # (check_fine_grained_processing), so nothing merges and no consumer
    # is spawned. The ordinary flavor closes a consumer by generating an
    # ES merge job, which has no place in a payload that registers per
    # range (job 3556339: every range done, the job failed on the merge
    # job's insert). Exclusive with nEventsPerWorker.
    if spec.get('fineGrainedProc'):
        params.pop('nEventsPerWorker', None)
        params['fineGrainedProc'] = True
    # An Event Service job runs its payload in one step outside the
    # pilot's container (the harness starts the image itself), so the
    # container's pre- and post-process steps have nothing to do: the
    # post-process runGen ran after the single-step one had removed its
    # work directory and failed, and every Event Service job carried
    # pilot error 1357 "Post-process command failed" though the server
    # finished it (jobs 3556341-3556349).
    if params.get('nEventsPerWorker') or params.get('fineGrainedProc'):
        params.pop('multiStepExec', None)

    # A PanDA input dataset (the ES shape that finishes: JEDI makes and
    # completes ranges over files of type input only, NODE_EVENT_DISPATCHER.md,
    # The round trip). The pilot stages the files in; the payload reads
    # what it reads. nEventsPerInputFile tells JEDI the events per file
    # when the catalog carries no event count.
    if spec.get('inputDataset'):
        params.pop('noInput', None)
        params['jobParameters'].append({
            'type': 'template', 'param_type': 'input',
            'value': '-i "${IN/T}"', 'dataset': spec['inputDataset'],
            'expand': True, 'exclude': r'\.log\.tgz(\.\d+)*$',
        })
        params['nFilesPerJob'] = int(spec.get('nFilesPerJob', 1))
        if spec.get('nEventsPerInputFile'):
            params['nEventsPerInputFile'] = int(spec['nEventsPerInputFile'])
        # Over real input nEvents is the events to process, not the job
        # count the noInput shape encodes in it.
        params['nEvents'] = params['nEventsPerJob'] * int(spec.get('nJobs', 1))
        # Input handed to the payload as a TURL, not staged: runGen's
        # --givenPFN takes the input names as given and skips its check
        # for a local copy, which otherwise ends the job before the
        # payload starts (job 3556333, "No input file is available"). The
        # pilot's own switch (--accessmode=direct) rides in the exec.
        if spec.get('directInput'):
            params['jobParameters'].append({'type': 'constant', 'value': '--givenPFN'})

    # Storage records the job carries for the pilot (--overwriteStorageData,
    # the pilot's job-level master source for StorageData): the pilot
    # otherwise knows storages only from ATLAS CRIC, and an Event Service
    # consumer must resolve the es_events storage to an id before it can
    # stage a range's receipt and report the range finished (job 3492644:
    # "Failed to load storage details for ddms=['BNL_PROD_DISK_1']" after
    # every range had been processed). The pilot tokenizes the job
    # parameters with shlex and takes the option's value as the next
    # token, read as a Python literal (jobdata.parse_args): the record
    # goes as a separate double-quoted token in Python's own syntax
    # (job 3493147 got it as one --option=json token and passed it to
    # runGen, which knows no such option).
    if spec.get('storageData'):
        record = repr(spec['storageData'])
        params['jobParameters'].append({
            'type': 'constant', 'value': f'--overwriteStorageData "{record}"'})

    # Files the pilot stages out and registers, by name (one job: the LFN
    # is the file's name in the job directory, no serial). Production
    # payloads self-register to JLab and use none of this; a PCS spec's
    # 'outputs' is its JLab datasets, another thing.
    if spec.get('stageOutFiles'):
        params.pop('noOutput', None)
        for name in spec['stageOutFiles']:
            params['jobParameters'].append({
                'type': 'template', 'param_type': 'output',
                'value': f"{spec['outDS']}.{name}",
                'dataset': f"{spec['outDS']}_{name.split('.')[0]}/",
                'hidden': True,
            })

    # Scouts off -> walltime used directly; scouts on -> HS06 per-event routing
    # (avoids the noInput pseudo-input 1MB-file walltime inflation).
    if spec.get('skipScout'):
        params['skipScout'] = True
    else:
        params['cpuTimeUnit'] = 'HS06sPerEvent'
    params['walltime'] = int(float(spec.get('walltimeHours', 2.0)) * 3600)

    # Job retry ceiling. Production leaves JEDI's default; a canary probe
    # sets 1 so a failed landing reads as a failed probe rather than being
    # blurred by automatic retries.
    if spec.get('maxAttempt'):
        params['maxAttempt'] = int(spec['maxAttempt'])

    if spec.get('containerImage'):
        params['container_name'] = spec['containerImage']

    # No log dataset at all (the prun --noSeparateLog shape): nothing for
    # the refiner to validate against Rucio and nothing for the Adder to
    # register. For queues whose log stage-out is an object store outside
    # the Rucio catalog, BNL_NPPS_GPU among them.
    if spec.get('noLog'):
        del params['log']

    # -a <sandbox>
    params['jobParameters'].append({'type': 'constant', 'value': f'-a {archive_name}'})

    # The job's own PanDA id, handed to the payload explicitly. The pilot
    # sets PANDAID in its own environment, but it launches the container
    # with --cleanenv, so nothing of the pilot's environment reaches the
    # payload; the server substitutes $PANDAID into job parameters
    # unconditionally, which does. The payload names the objects it
    # reports with it (swf-epicprod docs/JOB_REPORTING.md).
    exec_cmd = f"PANDAID=$PANDAID {spec['exec']}"
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
