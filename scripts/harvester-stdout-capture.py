#!/usr/bin/env python3
"""harvester-stdout-capture.py — copy the harvester's stdout of the jobs
worth keeping while the PanDA server still holds it.

The prod-ops agent's doer for ``harvester_stdout_capture``, hourly by
cron enqueue: every failed job (and finished, when the switch is on)
whose pilot id names a PanDA-cache stdout has that stdout copied whole
into our store, where the server's seven-day cache purge cannot reach
it; copies past ``harvester_stdout.keep_days`` are pruned (0 keeps
them) (docs/EPICPROD_OPS.md, Harvester stdout records).

Django-bootstrap standalone script, also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/harvester-stdout-capture.py [--days 7] [--limit N]
                                                  [--finished] [--prune] [--dry-run]
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

from monitor_app import harvester_stdout  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=float, default=None,
                        help='window of ended jobs to consider (default: the backfill_days setting, 7)')
    parser.add_argument('--limit', type=int, default=None, help='cap the candidates considered')
    parser.add_argument('--finished', action='store_true',
                        help='include finished jobs whatever the switch says')
    parser.add_argument('--prune', action='store_true', help='also prune copies past keep_days')
    parser.add_argument('--dry-run', action='store_true', help='with --prune, name the files only')
    parser.add_argument('--root', default=None, help='override the store root')
    args = parser.parse_args()

    root = args.root or harvester_stdout.store_root()
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)) if args.days else None
    print(f'store: {root}')
    counts, captured, gone = harvester_stdout.capture(
        since=since, finished=True if args.finished else None, limit=args.limit, root=root)
    print(f"candidates={counts['candidates']} captured={counts['captured']} gone={counts['gone']} "
          f"failed={counts['failed']} skipped={counts['skipped']} bytes={counts['bytes']}")
    if captured:
        print('captured: ' + ', '.join(str(p) for p in captured[:50]))
    if gone:
        print('gone from the cache before capture: ' + ', '.join(str(p) for p in gone[:50]))
    if args.prune:
        removed = harvester_stdout.prune(root=root, dry_run=args.dry_run)
        print(f"{'would remove' if args.dry_run else 'removed'} {len(removed)} file(s)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
