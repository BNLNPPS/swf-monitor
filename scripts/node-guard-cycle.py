#!/usr/bin/env python3
"""node-guard-cycle.py — one cycle of the node guard: the terminal
production jobs of the window per queue and host go to the canary's
decision, and the verdicts are recorded on the action stream and stored
for the Node guard page (site-canary docs/NODE_GUARD.md; logic in
monitor_app/panda/node_guard.py).

The prod-ops agent's doer for ``node_guard_cycle``, every five minutes
by cron enqueue. In shadow mode (SysConfig ``node_guard.mode``) it acts
on nothing and records what it would have done. Django-bootstrap
standalone script, also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/node-guard-cycle.py [--dry-run] [--created-by NAME]

``--dry-run`` judges and prints, and writes no record.
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

from monitor_app.panda.node_guard import run_cycle  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--dry-run', action='store_true',
                    help='judge and print, record nothing')
    ap.add_argument('--created-by', default='node-guard',
                    help='who runs the cycle (the records carry it)')
    args = ap.parse_args()
    try:
        nodes, summary = run_cycle(dry_run=args.dry_run, created_by=args.created_by)
    except Exception as exc:  # noqa: BLE001
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    for n in nodes:
        e = n['evidence']
        print(f"{n['queue']} {n['host']}: {n['state']} ({n['reason']}) "
              f"{e['failed']}/{e['jobs']} failed, {e['fast_failed']} fast, "
              f"{len(e['tasks_finished_elsewhere'])}/{len(e['tasks_failed'])} tasks finish elsewhere"
              f"{' recorded' if n['recorded'] and not args.dry_run else ''}")
    print('SUMMARY ' + json.dumps(summary, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
