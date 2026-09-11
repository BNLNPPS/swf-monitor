#!/usr/bin/env python3
"""segfault-repro-package.py — a crashed job's reproduction for a software expert.

Stage 4 of swf-epicprod docs/SEGFAULT_DIAGNOSIS.md. For one crashed job
of the segfault catalog this writes a directory, and a tarball of it,
holding everything an expert needs to run the crash again outside PanDA
and nothing they have to assemble: a README stating the crash, the
signature and the reproduction outcomes; ``run-repro.sh``, which runs
the campaign image through apptainer (or from inside eic-shell) with
the payload from this release, the job's own manifest row, its
environment and its seed, streaming the input from the JLab door by
path as the job did; the trace as found; and the job records of the
original and the reproductions. The package carries no credential: the
input path is public read, and the run registers nothing
(``CANARY_OUTPUT_DATASET`` unset, ``USERUCIO`` and the copies off).

Written under ``$SWF_TMP_DIR/segfault-packages/<key>/<pandaid>/``, the
tarball beside it, and the path recorded on the signature (``package``).

Django-bootstrap standalone script, run by hand or by the ops agent::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/segfault-repro-package.py 2723039

The last stdout line is a JSON summary; progress and errors go to stderr.
"""
import argparse
import json
import logging
import os
import shutil
import sys
import tarfile
import time
from datetime import datetime, timezone as dt_timezone

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from monitor_app.models import CrashSignature, EpicProdJob  # noqa: E402
from monitor_app.segfaults import SIGNAL_NAMES, signature_for_job  # noqa: E402

SWF_TMP_DIR = os.environ.get('SWF_TMP_DIR', '/data/swf-tmp')
PACKAGE_ROOT = os.path.join(SWF_TMP_DIR, 'segfault-packages')
log = logging.getLogger('segfault-repro-package')


def _payload_dir():
    """The payload as this release ships it (the submit doer's rule)."""
    import swf_epicprod
    return os.path.join(os.path.dirname(swf_epicprod.__file__), 'payload')


def _environment(prod_task):
    from pcs.commands import _evgen_env
    env = _evgen_env(prod_task)
    # No registration, no copies, no log upload: the run leaves its outputs
    # in the working directory and touches no catalog.
    env.update({'USERUCIO': 'false', 'COPYRECO': 'false', 'COPYFULL': 'false',
                'COPYLOG': 'false'})
    return env


def _container(prod_task):
    cfg = prod_task.prod_config
    return getattr(cfg, 'container_image', '') if cfg else ''


def build(pandaid):
    job = EpicProdJob.objects.filter(pandaid=pandaid, phase='payload_crash').select_related(
        'prod_task', 'prod_task__prod_config', 'prod_task__dataset').first()
    if job is None:
        raise ValueError(f'job {pandaid} is not in the crash inventory')
    crash = (job.data or {}).get('crash') or {}
    row = crash.get('row')
    if not row:
        raise ValueError(f"job {pandaid}'s manifest row is unresolved")
    if job.prod_task is None:
        raise ValueError(f'job {pandaid} has no PCS task')
    sig_summary = signature_for_job(pandaid)
    sig = CrashSignature.objects.filter(key=sig_summary['key']).first() if sig_summary else None
    key = sig.key if sig else f"exit{crash.get('exit_code')}:task{job.jeditaskid}"
    outdir = os.path.join(PACKAGE_ROOT, key.replace(':', '_'), str(pandaid))
    if os.path.isdir(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir)

    env = _environment(job.prod_task)
    container = _container(job.prod_task)
    row_text = f"{row['file']},{row['ext']},{row['nevents']},{int(row['ichunk']):04d}"
    with open(os.path.join(outdir, 'manifest.csv'), 'w') as fh:
        fh.write(row_text + '\n')
    with open(os.path.join(outdir, 'environment-manifest.sh'), 'w') as fh:
        for k, v in env.items():
            fh.write(f'export {k}={v}\n')
    shutil.copytree(_payload_dir(), os.path.join(outdir, 'payload'),
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy(os.path.join(THIS_DIR, 'evgen_job_dispatcher.py'),
                os.path.join(outdir, 'evgen_job_dispatcher.py'))
    with open(os.path.join(outdir, 'job-record.json'), 'w') as fh:
        json.dump({'pandaid': pandaid, 'jeditaskid': job.jeditaskid,
                   'seq_number': job.seq_number, 'prod_task': job.prod_task.name,
                   'crash': crash}, fh, indent=1, default=str)
    trace = (sig.trace if sig else {}) or {}
    with open(os.path.join(outdir, 'trace.txt'), 'w') as fh:
        fh.write(f"trace_status: {trace.get('trace_status', 'unknown')}\n")
        fh.write(f"program: {trace.get('program', '')}  stage: {trace.get('stage', '')}\n")
        fh.write(f"frame: {trace.get('frame', '')}  library: {trace.get('library', '')}\n")
        fh.write(f"source job: {trace.get('source_pandaid', '')}\n\n")
        for line in trace.get('frames') or []:
            fh.write(line + '\n')
        if trace.get('context'):
            fh.write('\n--- the lines before the crash ---\n')
            for line in trace['context']:
                fh.write(line + '\n')
    repro = (sig.reproduction if sig else []) or []
    with open(os.path.join(outdir, 'reproductions.json'), 'w') as fh:
        json.dump(repro, fh, indent=1, default=str)

    seed = int(row['ichunk']) + 1
    signal = crash.get('signal')
    with open(os.path.join(outdir, 'run-repro.sh'), 'w') as fh:
        fh.write(f'''#!/bin/bash
# Reproduce PanDA job {pandaid} (task {job.jeditaskid}, {job.prod_task.name}):
# the payload died on {SIGNAL_NAMES.get(signal, signal)} (exit {crash.get('exit_code')})
# after {crash.get('minutes')} min at {crash.get('computingsite')}.
#
# Runs the campaign image with the payload this package carries, on the
# job's own manifest row, with its seed ({seed} = chunk {int(row['ichunk'])} + 1),
# streaming the input from the JLab door as the job did. Outputs stay in
# the working directory; nothing is registered or uploaded.
#
# Usage: ./run-repro.sh            (apptainer on the host)
#        inside eic-shell or the image: ./run-repro.sh --inside
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="{container}"
if [ "${{1:-}}" != "--inside" ]; then
  if ! command -v apptainer >/dev/null 2>&1; then
    echo "apptainer not found; run inside the image with --inside" >&2; exit 2
  fi
  exec apptainer exec --bind "$HERE:$HERE" "$IMAGE" bash "$HERE/run-repro.sh" --inside
fi
cd "$HERE"
rm -rf work && mkdir work && cd work
cp ../manifest.csv manifest.csv
cp ../environment-manifest.sh environment-manifest.sh
cp -r ../payload payload
cp ../evgen_job_dispatcher.py .
export PAYLOAD_STAGES_LOG="$PWD/payload-stages.log"
echo "row: $(cat manifest.csv)"
python3 evgen_job_dispatcher.py 1 manifest || rc=$?
echo "payload exit: ${{rc:-0}}"
echo "stage log:"; cat payload-stages.log 2>/dev/null || true
exit ${{rc:-0}}
''')
    os.chmod(os.path.join(outdir, 'run-repro.sh'), 0o755)

    with open(os.path.join(outdir, 'README.md'), 'w') as fh:
        fh.write(f'''# Reproduction package: job {pandaid}

Signature {key}{' (' + sig_summary['class_label'] + ')' if sig_summary else ''}:
the payload died on {SIGNAL_NAMES.get(signal, signal)} (exit {crash.get('exit_code')})
after {crash.get('minutes')} minutes at {crash.get('computingsite')} on
{crash.get('modificationhost')}, peak resident memory {crash.get('maxrss_mb')} MB.
{'Stage ' + crash['stage'] + '. ' if crash.get('stage') else ''}The signature holds
{sig.crashes if sig else '?'} crashed jobs across {len(sig.tasks) if sig else '?'} task(s).

## The run

- PCS task: {job.prod_task.name}
- Container image: {container}
- Manifest row: {row_text}
- Seed: {seed} (chunk {int(row['ichunk'])} + 1; the payload enables the per-event seed)
- Payload: the epicprod payload in `payload/` (version file inside)
- Command: `./run-repro.sh` with apptainer on the host, or `./run-repro.sh --inside`
  from a shell inside the image or eic-shell. The input streams from the JLab
  door by path (public read); outputs stay in `work/`; nothing is registered.

## The trace

{('Crashing frame: ' + trace.get('frame', '') + (' in ' + trace['library'] if trace.get('library') else '') + ', read from job ' + str(trace.get('source_pandaid', ''))) if trace.get('trace_status') == 'found' else 'No trace on the record (' + str(trace.get('trace_status', 'unknown')) + '); the reproduction supplies it.'}

See `trace.txt`.

## Reproductions so far

{chr(10).join('- ' + str(r.get('queue')) + ': ' + str(r.get('outcome')) + (' (exit ' + str(r.get('exit_code')) + ')' if r.get('exit_code') is not None else '') for r in repro) or '- none yet'}

## Files

- `run-repro.sh`, `manifest.csv`, `environment-manifest.sh`, `payload/`, `evgen_job_dispatcher.py`: the run
- `trace.txt`: the crash trace as found
- `job-record.json`: the crashed job's record
- `reproductions.json`: the reproduction runs and their outcomes
''')
    tarball = outdir + '.tar.gz'
    with tarfile.open(tarball, 'w:gz') as tf:
        tf.add(outdir, arcname=f'segfault-repro-{pandaid}')
    if sig:
        sig.package = {'path': outdir, 'tarball': tarball, 'pandaid': pandaid,
                       'built_at': datetime.now(dt_timezone.utc).isoformat(timespec='seconds')}
        sig.save(update_fields=['package', 'updated_at'])
    return {'pandaid': pandaid, 'key': key, 'path': outdir, 'tarball': tarball,
            'bytes': os.path.getsize(tarball)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('pandaid', type=int, help='the crashed job')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    t0 = time.monotonic()
    try:
        summary = build(args.pandaid)
    except Exception as e:                                    # noqa: BLE001
        log.exception('package failed')
        print(json.dumps({'error': str(e)[:300]}))
        return 1
    summary['seconds'] = round(time.monotonic() - t0, 1)
    print(json.dumps(summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
