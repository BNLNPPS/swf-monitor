#!/usr/bin/env python3
"""batch-log-capture.py — capture condor event logs while they exist.

The prod-ops agent's doer for the batch-record capture: every failed
job's condor event log fetched whole from the harvester and written to
the file store, and date directories past the retention window removed.
The source keeps about eighteen days, so a log not captured is a reason
that cannot be recovered or re-parsed later
(docs/ERROR_ATTRIBUTION.md, Batch-layer records).

Django-bootstrap standalone script — also usable by hand. Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/batch-log-capture.py [--hours 24] [--limit N]
                                           [--prune] [--dry-run]
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from monitor_app import batch_records  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hours', type=float, default=24,
                        help='window of failed jobs to consider (default 24)')
    parser.add_argument('--limit', type=int, default=None,
                        help='cap the candidates considered, for a first run')
    parser.add_argument('--prune', action='store_true',
                        help='also remove date directories past retention')
    parser.add_argument('--dry-run', action='store_true',
                        help='with --prune, name the directories only')
    parser.add_argument('--root', default=None,
                        help='override the store root')
    args = parser.parse_args()

    root = args.root or batch_records.store_root()
    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    print(f'store: {root}')
    print(f'window: since {since.isoformat()}')

    counts = batch_records.capture(since=since, limit=args.limit, root=root)
    print(f"candidates={counts['candidates']} captured={counts['captured']} "
          f"failed={counts['failed']} skipped={counts['skipped']}")

    if args.prune:
        removed = batch_records.prune(root=root, dry_run=args.dry_run)
        verb = 'would remove' if args.dry_run else 'removed'
        print(f'{verb} {len(removed)} date directory(ies)')
        for path in removed:
            print(f'  {path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
