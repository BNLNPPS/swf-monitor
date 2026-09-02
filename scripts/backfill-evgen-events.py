#!/usr/bin/env python3
"""backfill-evgen-events.py — event counts for EVGEN datasets registered
before counting existed (docs/RUCIO_REGISTRATION_CONTRACT.md).

Reads the recorded EVGEN inventory snapshot (evgen-rucio.json), selects
the datasets whose Rucio ``events`` is unset, and runs the registration
doer's ``--events-only`` mode on each (register-evgen-rucio.py): the
per-file counts are read through the door and written as the files'
``events`` attribute, and the dataset's derived total is verified.
Dry run by default (counts, writes nothing); ``--apply`` writes.

Runs under the production operations agent's environment
(EVGEN_X509_PROXY and the door settings); ``--env-file`` loads a
systemd-style KEY=VALUE file when run by hand. After a run, the EVGEN
assimilation ("Update EVGEN from Rucio", or the nightly sweep) carries
the counts into the inventory.

Usage:
    backfill-evgen-events.py [--apply] [--env-file /opt/swf-monitor/config/env/production.env]
                             [--limit N] [--snapshot PATH]
"""
import argparse
import json
import os
import subprocess
import sys
import time

DOER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'register-evgen-rucio.py')
DEFAULT_SNAPSHOT = os.path.join(os.environ.get('SWF_TMP_DIR', '/data/swf-tmp'),
                                'rucio-snapshots', 'evgen-rucio.json')


def load_env_file(path):
    for line in open(path):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ.setdefault(key, value.strip().strip('"'))


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--apply', action='store_true', help='write the counts to Rucio')
    ap.add_argument('--env-file', help='KEY=VALUE file with the agent environment')
    ap.add_argument('--snapshot', default=DEFAULT_SNAPSHOT)
    ap.add_argument('--limit', type=int, default=0, help='stop after N datasets')
    args = ap.parse_args(argv[1:])
    if args.env_file:
        load_env_file(args.env_file)

    with open(args.snapshot) as f:
        snap = json.load(f)
    todo = [d['did'] for d in snap.get('datasets') or []
            if d.get('did') and d.get('events') in (None, 0)]
    if args.limit:
        todo = todo[:args.limit]
    print(f'{len(todo)} of {len(snap.get("datasets") or [])} EVGEN datasets without '
          f'a count; {"writing" if args.apply else "dry run"}', flush=True)

    results = []
    t_all = time.monotonic()
    for i, did in enumerate(todo, 1):
        path = '/' + did.partition(':')[2].lstrip('/')
        cmd = [sys.executable, DOER, '--path', path, '--events-only']
        if not args.apply:
            cmd.append('--dry-run')
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except subprocess.TimeoutExpired:
            results.append({'did': did, 'ok': False, 'error': 'timed out after 3600s'})
            print(f'[{i}/{len(todo)}] {did}: TIMEOUT', flush=True)
            continue
        try:
            summary = json.loads((p.stdout or '').strip().splitlines()[-1])
        except Exception:                                       # noqa: BLE001
            summary = {'ok': False, 'error': (p.stderr or '').strip().splitlines()[-1:]
                       or f'rc {p.returncode}'}
        rec = {'did': did, 'ok': bool(summary.get('ok')), 'events': summary.get('events'),
               'files': summary.get('files'), 'seconds': round(time.monotonic() - t0, 1),
               'error': summary.get('error', '')}
        rec['failed'] = [x for d in (summary.get('datasets') or []) for x in d.get('failed') or []]
        results.append(rec)
        print(f'[{i}/{len(todo)}] {did}: {"ok" if rec["ok"] else "FAILED"} '
              f'events={rec["events"]} files={rec["files"]} {rec["seconds"]}s'
              + (f' {rec["error"]}' if rec['error'] else ''), flush=True)
        for x in rec['failed'][:5]:
            print(f'      {x["file"]}: {x["reason"]}', flush=True)

    ok = sum(1 for r in results if r['ok'])
    total_events = sum(r['events'] or 0 for r in results if r['ok'])
    print(json.dumps({'datasets': len(results), 'ok': ok, 'failed': len(results) - ok,
                      'events_total': total_events, 'apply': args.apply,
                      'seconds': round(time.monotonic() - t_all, 1),
                      'failures': [{'did': r['did'], 'error': r['error'],
                                    'files': r['failed'][:5]}
                                   for r in results if not r['ok']]}), flush=True)
    return 0 if ok == len(results) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
