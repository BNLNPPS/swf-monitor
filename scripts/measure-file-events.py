#!/usr/bin/env python3
"""measure-file-events.py — per-file event measurement for delivered
campaign data.

The prod-ops agent's doer for the nightly measurement pass (a step of
the ``catalog_sync`` chain): anchor each new byte-size class with one
xrootd read on a disk replica — never tape — and fill class members at
the anchored rate; tape-only classes of dormant complete sources derive
from the ANL catalog. Incremental: already-measured files are skipped,
so the nightly pass costs a few file opens at most. Logic in
``swf_epicprod/analytics/file_events.py``; see
``swf-epicprod/docs/CAMPAIGN_DELIVERY.md`` (The events source).
Django-bootstrap standalone script — also usable by hand.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/measure-file-events.py [--workers 6]
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

from swf_epicprod.analytics.file_events import measure_file_events  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--campaigns', default='',
                        help='comma-separated campaign families '
                             '(default: all in the catalog)')
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--locations', type=int, default=0,
                        help='process at most N locations (0 = all)')
    args = parser.parse_args()
    campaigns = [c.strip() for c in args.campaigns.split(',')
                 if c.strip()] or None

    stats = measure_file_events(campaigns, workers=args.workers,
                                max_locations=args.locations)
    print('SUMMARY ' + json.dumps(stats))
    return 0


if __name__ == '__main__':
    sys.exit(main())
