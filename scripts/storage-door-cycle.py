#!/usr/bin/env python3
"""storage-door-cycle.py — one cycle of the storage door canary: the
doors production writes through, from the catalog, used rather than
inspected — a small write, a stat, a delete, and the date on the
certificate they serve — and the verdicts recorded for the page and for
production operations (site-canary docs/STORAGE_DOORS.md; logic in
monitor_app/panda/storage_doors.py).

The prod-ops agent's doer for ``storage_door_cycle``, every fifteen
minutes by cron enqueue; a door is probed when its last probe is older
than ``storage_doors.interval_h`` (one hour by default), so the cadence
is a setting. Nothing acts on the record automatically: a door that
changes verdict raises its action, at alarm severity when it goes down,
and production operations decide what follows.

Django-bootstrap standalone script, also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/storage-door-cycle.py [--dry-run] [--force]

``--dry-run`` probes and prints, and writes no record. ``--force``
probes every door whatever the interval and whatever the switch says,
for a look by hand; it still records unless ``--dry-run`` says not to.
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

from monitor_app.panda.storage_doors import run_cycle  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--dry-run', action='store_true',
                    help='probe and print, record nothing')
    ap.add_argument('--force', action='store_true',
                    help='probe every door, whatever the interval or the switch')
    ap.add_argument('--created-by', default='storage-doors',
                    help='who runs the cycle (the records carry it)')
    args = ap.parse_args()
    try:
        doors, summary = run_cycle(dry_run=args.dry_run, force=args.force,
                                   created_by=args.created_by)
    except Exception as exc:  # noqa: BLE001
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    for rse, state in sorted(doors.items()):
        evidence = state.get('evidence') or {}
        certificate = (f", certificate {evidence['certificate']} "
                       f"({evidence['certificate_days_left']} days)"
                       if evidence.get('certificate') else '')
        print(f"{rse:12} {state.get('verdict', '-'):8} "
              f"{state.get('reason', ''):20} {state.get('door', '')}{certificate}")
    print('SUMMARY ' + json.dumps(summary, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
