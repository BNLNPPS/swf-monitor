#!/usr/bin/env python3
"""segfault-reproductions-reconcile.py — reproduction requests meet their runs.

The step the canary agent runs after each probe collection
(swf-epicprod docs/SEGFAULT_DIAGNOSIS.md, Reproduction;
monitor_app/reproductions.py): every open reproduction request on a
crash signature takes the identity and outcome of the canary run that
answered it, a production run and a reference run that have both
reported settle the signature's reproduction outcome, and a signature
settled as reproduced or site dependent with a trace on record has its
diagnosis queued, once. Nothing here reaches PanDA: the runs' evidence
is what the collection already wrote to the canary store.

Django-bootstrap standalone script, by hand::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/segfault-reproductions-reconcile.py [--key KEY] [--dry-run]

Prints one JSON line: the signatures visited and what changed on each.
Exit status 1 when any signature's reconciliation raised.
"""
import argparse
import json
import logging
import os
import sys
import time

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from monitor_app.epicprod_logging import log_epicprod_action  # noqa: E402
from monitor_app.reproductions import attempts, open_signature_keys, reconcile  # noqa: E402
from monitor_app.segfaults import queue_diagnosis  # noqa: E402

log = logging.getLogger('segfault-reproductions-reconcile')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--key', help='one signature instead of every open one')
    ap.add_argument('--dry-run', action='store_true',
                    help='show the attempts of the open signatures, write nothing')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    t0 = time.monotonic()
    keys = [args.key] if args.key else open_signature_keys()
    if args.dry_run:
        rows = attempts(keys=keys)
        print(json.dumps({'dry_run': True, 'signatures': keys,
                          'attempts': [{k: r[k] for k in ('signature', 'queue', 'phase', 'result',
                                                          'reason', 'jeditaskid', 'canary_pandaid',
                                                          'run_id', 'request_id')}
                                       for r in rows]}, default=str))
        return 0
    results = {}
    errors = 0
    for key in keys:
        try:
            results[key] = reconcile(key, queue_diagnosis=queue_diagnosis)
        except Exception as e:                                # noqa: BLE001
            log.exception('reconcile %s failed', key)
            results[key] = {'error': str(e)[:300]}
            errors += 1
    changed = {k: v for k, v in results.items()
               if v.get('entries') or v.get('outcome') or v.get('error')}
    if changed:
        settled = [k for k, v in changed.items() if v.get('outcome')]
        queued = [k for k, v in changed.items() if v.get('diagnosis')]
        log_epicprod_action(
            'canary-agent', 'segfault_reproduction_reconcile',
            outcome='error' if errors else 'ok',
            sublevel='low', live_default=False,
            level=logging.ERROR if errors else logging.INFO,
            duration_ms=int((time.monotonic() - t0) * 1000),
            message=(f"reproduction reconcile: {len(changed)} signature(s) changed"
                     + (f", settled {', '.join(settled)}" if settled else '')
                     + (f", diagnosis queued for {', '.join(queued)}" if queued else '')
                     + (f", {errors} error(s)" if errors else '')),
            visited=len(keys), changed=len(changed), settled=len(settled),
            diagnoses=len(queued), errors=errors)
    print(json.dumps({'visited': len(keys), 'changed': changed}, default=str))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
