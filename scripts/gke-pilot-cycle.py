#!/usr/bin/env python3
"""gke-pilot-cycle.py — one cycle of the BNL_ePIC_GOOGLE_es pilot flow:
the queue's activated jobs and our pilot pods in the GKE cluster decide
how many pilot pods to start, and the cycle starts them
(swf-epicprod docs/GKE_PILOT_FLOW.md; logic in swf_epicprod/gke_pilots.py).

The prod-ops agent's doer for ``gke_pilot_cycle``, enqueued by cron.
It starts nothing while SysConfig ``gke_pilots.*`` is incomplete.
Django-bootstrap standalone script, also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/gke-pilot-cycle.py [--dry-run] [--created-by NAME]

``--dry-run`` decides and prints, starting and recording nothing.
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

from swf_epicprod.gke_pilots import run_cycle  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--dry-run', action='store_true',
                    help='decide and print, start and record nothing')
    ap.add_argument('--created-by', default='gke_pilots',
                    help='who runs the cycle (the record carries it)')
    args = ap.parse_args()
    try:
        summary = run_cycle(dry_run=args.dry_run, created_by=args.created_by)
    except Exception as exc:  # noqa: BLE001
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    print(f"{summary['queue']}: {summary['outcome']} ({summary['reason']}); "
          f"activated {summary['activated']}, started {summary['started']}")
    print('SUMMARY ' + json.dumps(summary, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
