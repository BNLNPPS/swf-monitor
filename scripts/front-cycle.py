#!/usr/bin/env python3
"""front-cycle.py — one decision cycle of the pressure front: for each
regulated queue, the census, the gates and the ready backlog decide
feed or hold with a reason, recorded on the action stream
(swf-epicprod docs/CONTINUOUS_PRODUCTION.md, The dispatcher; logic in
swf_epicprod/front.py).

The prod-ops agent's doer for ``front_cycle``, every five minutes by
cron enqueue. In shadow mode (SysConfig ``front.mode``) it submits
nothing and records what it would have done. Django-bootstrap
standalone script, also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/front-cycle.py [--dry-run] [--created-by NAME]

``--dry-run`` prints every queue's decision and writes no record.
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

from swf_epicprod.front import run_cycle  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--dry-run', action='store_true',
                    help='decide and print, record nothing')
    ap.add_argument('--created-by', default='front',
                    help='who runs the cycle (the records carry it)')
    args = ap.parse_args()
    try:
        decisions, summary = run_cycle(dry_run=args.dry_run,
                                       created_by=args.created_by)
    except Exception as exc:  # noqa: BLE001
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    for d in decisions:
        depth = d['committed_h'] if d['committed_h'] is not None else 'n/a'
        print(f"{d['queue']}: {d['state']} ({d['reason']}) committed {depth} h "
              f"of {d['h_low']}-{d['h_high']}; ready {d['ready_eligible']}/{d['ready_total']}"
              f"{' recorded' if d['recorded'] and not args.dry_run else ''}")
    print('SUMMARY ' + json.dumps(summary, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
