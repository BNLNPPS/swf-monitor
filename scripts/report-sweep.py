#!/usr/bin/env python3
"""report-sweep.py — sweep failed jobs' payload reports out of the store.

The prod-ops agent's doer for the payload report sweep: a bounded sample
per failure signature read and filed beside the job record, the rest of
that signature deleted unread, and a pass record posted to the gateway
whatever the outcome (swf-epicprod docs/JOB_REPORTING.md).

Django-bootstrap standalone script — also usable by hand. Usage::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/report-sweep.py \
        [--hours 6] [--limit N] [--per-signature 2] [--dry-run]

--dry-run reads and reports but files nothing, deletes nothing and posts
no pass record, so it can be run against production to see what a pass
would take.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from monitor_app import payload_reports  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hours', type=float,
                        default=payload_reports.DEFAULT_HOURS,
                        help='window of failed jobs to consider')
    parser.add_argument('--limit', type=int, default=None,
                        help='cap the candidates considered, for a first run')
    parser.add_argument('--per-signature', type=int,
                        default=payload_reports.READ_PER_SIGNATURE,
                        help='reports read per distinct failure signature')
    parser.add_argument('--dry-run', action='store_true',
                        help='read only: file nothing, delete nothing, post nothing')
    parser.add_argument('--json', action='store_true',
                        help='print the pass record as JSON')
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    print(f'window: since {since.isoformat()}')
    record = payload_reports.sweep(
        since, limit=args.limit, per_signature=args.per_signature,
        dry_run=args.dry_run)
    if args.json:
        print(json.dumps(record, indent=2, default=str))
    print(f"outcome={record['outcome']} candidates={record.get('candidates', 0)} "
          f"signatures={record.get('signatures', 0)} filed={len(record['filed'])} "
          f"deleted_read={len(record['deleted_read'])} "
          f"deleted_unread={len(record['deleted_unread'])} "
          f"delivered={record.get('delivered')}")
    if record.get('reason'):
        print(f"reason: {record['reason']}")
    return 0 if record['outcome'] != 'failed' else 1


if __name__ == '__main__':
    sys.exit(main())
