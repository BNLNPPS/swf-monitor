#!/usr/bin/env python3
"""segfault-es-reproduce.py — an Event Service unit's crash request replayed on npps0.

The reproduction of an Event Service unit signature (swf-epicprod
docs/SEGFAULT_DIAGNOSIS.md, Event Service units). A crashed unit's events
went back to their file and its job ended finished, so no payload canary
replays it: the request the slot sent its resident simulation (input, first
event, count, seed) is run again in the job's campaign image with the
payload of the job's version, on npps0, the reference host of known
conditions, through the npps0 route (the npps0 skill's npps0.sh). Never on
the monitor host.

``scripts/segfault-es-replay.sh`` is the driver on npps0: run.sh over the
unit as a slot runs its first unit, then, when the resident dies, the
request's arguments once more as a cold npsim under gdb. This doer:

1. picks the signature's most repeated crash request, or the one --unit
   names (its 1-based place in the signature's crash requests);
2. records the attempt on the signature (mode host, queue npps0, role
   reference), so the runs page shows it while it runs;
3. ships the payload of the job's version (the commit that set that version
   in payload/VERSION), the task's payload environment and the driver to
   npps0, starts the driver detached and polls for its result;
4. fetches the result and logs into
   ``$SWF_TMP_DIR/segfault-packages/<key>/es-replay-<request id>/``;
5. records the outcome on the entry and, for a crash, the signal on the
   signature and the trace read from gdb's backtrace (``record_trace``,
   merged by frame as a dig's), then reconciles the signature, which
   settles it and queues its study or marks it covered by a finding.

Django-bootstrap standalone script, by hand::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/segfault-es-reproduce.py --key esunit:task41497:simu

``--dry-run`` prints the plan and writes nothing. Progress goes to stderr;
the last stdout line is a JSON summary. Exit status 1 on an error.
"""
import argparse
import io
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone as dt_timezone

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from monitor_app.epicprod_inventory import trace_extract  # noqa: E402
from monitor_app.epicprod_logging import log_epicprod_action  # noqa: E402
from monitor_app.models import CrashSignature  # noqa: E402
from monitor_app.reproductions import new_request_id, reconcile, update_host_entry  # noqa: E402
from monitor_app.segfaults import (PAYLOAD_VERSION_FILE, _append_reproduction, is_es_unit,  # noqa: E402
                                   queue_diagnosis, record_trace, repro_environment)

NPPS0 = os.path.expanduser(os.environ.get('NPPS0_ROUTE', '~/.claude/skills/npps0/npps0.sh'))
HOST = 'npps0'
REMOTE_ROOT = 'work/segfault-es'           # under wenaus's home on npps0
SWF_TMP_DIR = os.environ.get('SWF_TMP_DIR', '/data/swf-tmp')
DRIVER = os.path.join(THIS_DIR, 'segfault-es-replay.sh')
# What the driver leaves that the record keeps.
EVIDENCE = ['replay.out', 'out/result.json', 'out/payload.log', 'out/stages.log', 'out/gdb.log',
            'out/simulation.json', 'out/unit-template.json.npsim.json', 'state/resident.log',
            'gdb/args.log', 'gdb/cut.log', 'gdb/range.hepmc3.json']
log = logging.getLogger('segfault-es-reproduce')


def npps0(command, stdin=None, timeout=300):
    """Run a command on npps0; returns (rc, stdout bytes, stderr text)."""
    p = subprocess.run([NPPS0, command], input=stdin, capture_output=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr.decode(errors='replace')


def payload_repo():
    """The swf-epicprod clone the payload versions are resolved in."""
    import swf_epicprod
    here = os.path.dirname(swf_epicprod.__file__)
    p = subprocess.run(['git', '-C', here, 'rev-parse', '--show-toplevel'],
                       capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(f'swf_epicprod at {here} is not a git checkout: {p.stderr.strip()}')
    return p.stdout.strip()


def version_commit(repo, version):
    """The commit that set ``version`` in payload/VERSION (local_repro's rule)."""
    p = subprocess.run(['git', '-C', repo, 'log', f'-S{version}', '--format=%H', '--',
                        PAYLOAD_VERSION_FILE], capture_output=True, text=True)
    commits = p.stdout.split()
    if p.returncode or not commits:
        raise RuntimeError(f'no commit sets {version!r} in {PAYLOAD_VERSION_FILE}')
    return commits[-1]


def plan(sig, unit_index):
    """What the replay runs: the request, the task, the image, the payload
    version, the run.sh arguments and the environment."""
    from pcs.models import PandaTasks
    units = (sig.data or {}).get('units') or []
    if not units:
        raise ValueError(f'{sig.key} records no crash request')
    if not 1 <= unit_index <= len(units):
        raise ValueError(f'--unit {unit_index}: {sig.key} has {len(units)} crash requests')
    unit = units[unit_index - 1]
    if None in (unit.get('first_event'), unit.get('events'), unit.get('seed')) or not unit.get('lfn'):
        raise ValueError(f'crash request {unit_index} lacks its input, first event, count or seed')
    tid = int((sig.tasks or [{}])[0].get('jeditaskid') or 0)
    attempt = PandaTasks.objects.filter(jedi_task_id=tid).select_related(
        'prod_task', 'prod_task__prod_config').first()
    if attempt is None or attempt.prod_task is None:
        raise ValueError(f'task {tid} has no PCS attempt record')
    md = attempt.metadata or {}
    version = str(md.get('payload_version') or '')
    if not version:
        raise ValueError(f'task {tid} records no payload version')
    files = ((md.get('manifest') or {}).get('files')) or []
    match = next((f for f in files
                  if f"{os.path.basename(f['file'])}.{f['ext']}" == unit['lfn']), None)
    if match is None:
        raise ValueError(f"{unit['lfn']} is not in task {tid}'s manifest")
    first, count, seed = int(unit['first_event']), int(unit['events']), int(unit['seed'])
    # run.sh seeds a chunk with chunk + 1 (hepmc3 input); a slot names a unit
    # that does not open its block by the block and its start (es_slot.py).
    chunk = seed - 1
    unit_events = int((md.get('event_service') or {}).get('unit_events') or 0)
    label = '' if first == chunk * (unit_events or count) else f'{chunk:04d}s{first + 1}'
    container = (sig.configuration or {}).get('container_image') or getattr(
        attempt.prod_task.prod_config, 'container_image', '')
    if not container:
        raise ValueError(f'task {tid} records no container image')
    env = repro_environment(attempt.prod_task)
    # The job's resident simulation of external EVGEN (production_es.apply).
    env.update({'EPICPROD_SWITCH_RESIDENT_SIM': '1', 'EPICPROD_SWITCH_RESIDENT_EVGEN': '1'})
    return {'key': sig.key, 'jeditaskid': tid, 'task': attempt.prod_task.name,
            'unit_index': unit_index, 'unit': {k: unit.get(k) for k in
                                               ('lfn', 'first_event', 'events', 'seed', 'passes')},
            'crashed_jobs': sorted({c['pandaid'] for c in unit.get('crashes') or []}),
            'container': container, 'payload_version': version,
            'run_args': [f"EVGEN/{match['file']}", match['ext'], str(count), f'{chunk:04d}'],
            'skip': first, 'label': label, 'environment': env}


def bundle(p, commit, repo, timeout_s):
    """The tarball shipped to npps0: payload/, environment-replay.sh,
    replay.env and the driver."""
    arch = subprocess.run(['git', '-C', repo, 'archive', '--format=tar', commit,
                           'swf_epicprod/payload'], capture_output=True)
    if arch.returncode:
        raise RuntimeError(f'git archive {commit}: {arch.stderr.decode(errors="replace").strip()}')
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz') as dst, \
            tarfile.open(fileobj=io.BytesIO(arch.stdout), mode='r:') as src:
        for member in src.getmembers():
            if not member.name.startswith('swf_epicprod/payload'):
                continue
            member.name = member.name[len('swf_epicprod/'):]
            dst.addfile(member, src.extractfile(member) if member.isfile() else None)

        def add(name, text, mode=0o644):
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), mode, int(time.time())
            dst.addfile(info, io.BytesIO(data))

        add('environment-replay.sh',
            ''.join(f'export {k}={shlex.quote(str(v))}\n' for k, v in p['environment'].items()))
        file, ext, events, chunk = p['run_args']
        add('replay.env', ''.join(f'export {k}={shlex.quote(str(v))}\n' for k, v in (
            ('REPLAY_FILE', file), ('REPLAY_EXT', ext), ('REPLAY_EVENTS', events),
            ('REPLAY_CHUNK', chunk), ('REPLAY_SKIP', p['skip']), ('REPLAY_LABEL', p['label']),
            ('REPLAY_SEED', p['unit']['seed']), ('REPLAY_TIMEOUT_S', timeout_s))))
        with open(DRIVER) as fh:
            add('segfault-es-replay.sh', fh.read(), 0o755)
    return out.getvalue()


def frames_of(gdb_text):
    """The trace in gdb's log, through the shared reader."""
    if not gdb_text:
        return None
    result = trace_extract([gdb_text])
    return result if result.get('trace_status') == 'found' else None


# DD4hep's handler names each signal the resident takes (state/resident.log).
CRASH_SIGNAL_RE = re.compile(r'Handle signal: (6|7|8|11)\b')


def resident_reading(local, result):
    """(signal, outcome, reason) from the resident's own log as fetched: the
    first crash-class signal is the crash; a SIGTERM after it is the request
    timeout ending a resident that stalled after its signal; a SIGTERM alone
    is no crash (41494, 2026-10-29: SIGABRT, a stall, then SIGTERM at
    1,800 s, which a driver reading the last signal took for the crash)."""
    path = os.path.join(local, 'state', 'resident.log')
    text = open(path, errors='replace').read() if os.path.exists(path) else ''
    killed = 'Handle signal: 15' in text
    m = CRASH_SIGNAL_RE.search(text)
    if m:
        signal = int(m.group(1))
        return signal, 'crashed', (f'resident npsim died on signal {signal}'
                                   + (', stalled after it and was killed at the request timeout'
                                      if killed else ''))
    if result.get('outcome') == 'crashed' or killed:
        return None, 'inconclusive', ('the resident was killed at the request timeout '
                                      'with no crash signal')
    return result.get('signal'), result['outcome'], result.get('reason') or ''


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=dt_timezone.utc).isoformat() if epoch else None


def run(args):
    sig = CrashSignature.objects.filter(key=args.key).first()
    if sig is None:
        raise ValueError(f'no signature {args.key}')
    if not is_es_unit(sig):
        raise ValueError(f'{args.key} is not an Event Service unit signature')
    p = plan(sig, args.unit)
    repo = payload_repo()
    commit = version_commit(repo, p['payload_version'])
    p['payload_commit'] = commit
    if args.dry_run:
        return dict(p, dry_run=True, environment=sorted(p['environment']))
    rid = new_request_id()
    name = f"task{p['jeditaskid']}-{p['unit']['first_event']}-{rid}"
    remote = f'{REMOTE_ROOT}/{name}'
    local = os.path.join(SWF_TMP_DIR, 'segfault-packages', args.key, f'es-replay-{rid}')
    os.makedirs(local, exist_ok=True)
    pandaid = p['crashed_jobs'][0] if p['crashed_jobs'] else None
    _append_reproduction(args.key, {
        'request_id': rid, 'mode': 'host', 'queue': HOST, 'host': HOST, 'role': 'reference',
        'pandaid': pandaid, 'unit': p['unit'], 'container': p['container'],
        'payload_version': p['payload_version'], 'remote_dir': remote, 'evidence': local,
        'requested_at': datetime.now(dt_timezone.utc).isoformat(),
        'requested_by': args.requested_by, 'outcome': 'running'})
    try:
        rc, _out, err = npps0(f'mkdir -p ~/{remote} && tar -xz -C ~/{remote}',
                              stdin=bundle(p, commit, repo, args.timeout))
        if rc:
            raise RuntimeError(f'shipping to npps0 failed (exit {rc}): {err.strip()[-300:]}')
        # Detached with every stream away from the session, so the ssh
        # returns at once (a plain nohup ... & kept the session open).
        inner = (f'exec apptainer exec -B /cvmfs {shlex.quote(p["container"])} '
                 f'bash ./segfault-es-replay.sh > replay.out 2>&1 < /dev/null')
        rc, _out, err = npps0(f'cd ~/{remote} && setsid -f bash -c {shlex.quote(inner)} '
                              f'> /dev/null 2>&1 < /dev/null; echo started', timeout=120)
        if rc:
            raise RuntimeError(f'starting the driver failed (exit {rc}): {err.strip()[-300:]}')
    except Exception as e:                                    # noqa: BLE001
        update_host_entry(args.key, rid, {'outcome': 'inconclusive',
                                          'reason': f'replay failed: {str(e)[:250]}',
                                          'ended_at': datetime.now(dt_timezone.utc).isoformat()})
        raise
    log.info('%s: request %s replaying on npps0 in ~/%s', args.key, rid, remote)
    update_host_entry(args.key, rid, {'started_at': datetime.now(dt_timezone.utc).isoformat()})
    return collect(args, rid, remote, local, pandaid)


def collect(args, rid, remote, local, pandaid):
    """Poll npps0 for the driver's result, fetch the evidence, record the
    outcome and the trace on the signature and reconcile it."""
    t0 = time.time()
    try:
        result = None
        deadline = time.time() + 2 * args.timeout + 600
        while time.time() < deadline:
            try:
                rc, out, err = npps0(f'cat ~/{remote}/out/result.json 2>/dev/null; '
                                     f'tail -1 ~/{remote}/out/stages.log 2>/dev/null >&2', timeout=120)
            except subprocess.TimeoutExpired:
                # One slow look at the route is not the run's end.
                log.warning('%s: a poll of npps0 timed out; trying again', args.key)
                time.sleep(args.poll)
                continue
            if out.strip():
                result = json.loads(out)
                break
            log.info('%s: running; %s', args.key, err.strip()[-200:] or 'no stage yet')
            time.sleep(args.poll)
        files = ' '.join(EVIDENCE)
        rc, tgz, err = npps0(f'cd ~/{remote} && tar -cz --ignore-failed-read {files} 2>/dev/null',
                             timeout=300)
        if tgz:
            os.makedirs(local, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(tgz), mode='r:gz') as t:
                t.extractall(local, filter='data')
        if result is None:
            raise RuntimeError(f'no result from npps0 within {2 * args.timeout + 600} s '
                               f'(the driver may still run in ~/{remote}; --collect {rid} resumes)')
    except Exception as e:                                    # noqa: BLE001
        update_host_entry(args.key, rid, {'outcome': 'inconclusive',
                                          'reason': f'replay failed: {str(e)[:250]}',
                                          'ended_at': datetime.now(dt_timezone.utc).isoformat()})
        raise
    signal, outcome, reason = resident_reading(local, result)
    if result.get('gdb', {}).get('stopped_on'):
        reason += f"; the cold npsim under gdb stopped on {result['gdb']['stopped_on']}"
    fields = {'outcome': outcome, 'reason': reason,
              'exit_code': 128 + signal if signal else result.get('payload_exit'),
              'signal': signal, 'started_at': iso(result.get('started_at')),
              'ended_at': iso(result.get('ended_at')),
              'minutes': round((result['ended_at'] - result['started_at']) / 60.0, 1)
              if result.get('ended_at') and result.get('started_at') else None,
              'gdb': result.get('gdb'), 'simulation_reply': result.get('simulation_reply')}
    trace = None
    if outcome == 'crashed':
        gdb_log = os.path.join(local, 'out', 'gdb.log')
        trace = frames_of(open(gdb_log, errors='replace').read() if os.path.exists(gdb_log) else '')
        if trace is None:
            fields['reason'] += '; the cold npsim under gdb gave no backtrace'
    update_host_entry(args.key, rid, fields)
    merged = None
    if signal:
        # The signal is named now (SEGFAULT_DIAGNOSIS.md, Event Service units,
        # Signature): set before the trace, whose frame key carries the exit.
        sig = CrashSignature.objects.get(key=args.key)
        if sig.signal != signal:
            sig.signal, sig.exit_code = signal, 128 + signal
            sig.save(update_fields=['signal', 'exit_code', 'updated_at'])
    if trace is not None:
        sig = CrashSignature.objects.get(key=args.key)
        merged = record_trace(sig, trace, pandaid, '')
    changed = reconcile(args.key, queue_diagnosis=queue_diagnosis)
    return {'key': args.key, 'request_id': rid, 'outcome': outcome, 'signal': signal,
            'frame': (trace or {}).get('frame'), 'library': (trace or {}).get('library'),
            'merged_into': merged, 'reconcile': changed, 'evidence': local,
            'remote_dir': f'npps0:~/{remote}', 'seconds': int(time.time() - t0)}


def resume(args):
    """--collect: take up a host attempt already running on npps0 from its
    entry (a doer that was interrupted leaves the driver running)."""
    sig = CrashSignature.objects.filter(key=args.key).first()
    entry = next((e for e in (sig.reproduction or []) if e.get('request_id') == args.collect
                  and e.get('mode') == 'host'), None) if sig else None
    if entry is None:
        raise ValueError(f'{args.key} has no host attempt {args.collect}')
    if entry.get('outcome') != 'running' and not args.again:
        raise ValueError(f"attempt {args.collect} has ended ({entry.get('outcome')}); "
                         f"--again records it once more from its evidence")
    return collect(args, args.collect, entry['remote_dir'], entry['evidence'], entry.get('pandaid'))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--key', required=True, help='an esunit: signature')
    ap.add_argument('--unit', type=int, default=1,
                    help="the crash request's 1-based place among the signature's "
                         '(default 1, the most repeated)')
    ap.add_argument('--timeout', type=int, default=3600,
                    help='seconds for run.sh, and again for gdb, on npps0 (default 3600)')
    ap.add_argument('--poll', type=int, default=30, help='seconds between result checks')
    ap.add_argument('--requested-by', default='swf-testbed session')
    ap.add_argument('--dry-run', action='store_true', help='print the plan, write nothing')
    ap.add_argument('--collect', metavar='REQUEST_ID',
                    help='take up the running host attempt REQUEST_ID of --key instead of starting one')
    ap.add_argument('--again', action='store_true',
                    help='--collect: record an ended attempt once more from its evidence on npps0')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    t0 = time.monotonic()
    try:
        summary = resume(args) if args.collect else run(args)
    except Exception as e:                                    # noqa: BLE001
        log.exception('replay of %s failed', args.key)
        if not args.dry_run:
            log_epicprod_action('segfault-es-reproduce', 'segfault_es_reproduction', outcome='error',
                                subject_type='crash_signature', subject_key=args.key,
                                username=args.requested_by, sublevel='normal', live_default=False,
                                level=logging.ERROR, duration_ms=int((time.monotonic() - t0) * 1000),
                                message=f'Event Service replay of {args.key} on npps0 failed: {str(e)[:200]}',
                                reason=str(e)[:300])
        print(json.dumps({'key': args.key, 'error': str(e)[:300]}))
        return 1
    if not args.dry_run:
        log_epicprod_action(
            'segfault-es-reproduce', 'segfault_es_reproduction', outcome='ok',
            subject_type='crash_signature', subject_key=args.key, username=args.requested_by,
            sublevel='normal', live_default=False, duration_ms=int((time.monotonic() - t0) * 1000),
            message=(f"Event Service replay of {args.key} on npps0: {summary['outcome']}"
                     + (f", signal {summary['signal']}" if summary.get('signal') else '')
                     + (f", frame {summary['frame']}" if summary.get('frame') else '')),
            request_id=summary['request_id'], result=summary['outcome'],
            frame=summary.get('frame') or '')
    print(json.dumps(summary, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
