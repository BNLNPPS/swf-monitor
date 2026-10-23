#!/usr/bin/env python3
"""es-closeout-cycle.py — one cycle of the preemption close-out: a running
Event Service job at a queue listed in SysConfig ``es_closeout.queues``
whose shipped record has gone quiet is credited and finished (swf-epicprod
docs/NODE_EVENT_DISPATCHER.md, Preemption; logic in
swf_epicprod/es_closeout.py).

The prod-ops agent's doer for ``es_closeout_cycle``, every five minutes by
cron enqueue. Django-bootstrap standalone script, also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/es-closeout-cycle.py [--dry-run] [--created-by NAME]

``--dry-run`` judges and prints, and closes and records nothing.
Prints one ``SUMMARY`` JSON line; exit 1 on failure.
"""
import argparse
import json
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from swf_epicprod.es_closeout import run_cycle  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--dry-run', action='store_true',
                    help='judge and print, close and record nothing')
    ap.add_argument('--created-by', default='es-closeout',
                    help='who runs the cycle (the records carry it)')
    args = ap.parse_args()
    try:
        decisions, summary = run_cycle(dry_run=args.dry_run, created_by=args.created_by)
    except Exception as exc:  # noqa: BLE001
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    for d in decisions:
        r = d.get('result') or {}
        outcome = ('' if 'result' not in d else
                   ' closed' if r.get('ok') else f" NOT closed: {r.get('refused') or r.get('error') or r.get('update')}")
        print(f"{d['pandaid']} {d['queue']}: {'quiet' if d.get('act') else 'live'} ({d['reason']}){outcome}")
    print('SUMMARY ' + json.dumps(summary, default=str))
    return 1 if summary.get('failed') else 0


if __name__ == '__main__':
    sys.exit(main())
