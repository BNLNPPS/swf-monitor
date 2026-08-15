#!/usr/bin/env python3
"""delivery-daily-rebuild.py — rebuild the campaign delivered-data
daily record.

The prod-ops agent's doer for the nightly daily-record rebuild (a step
of the ``catalog_sync`` chain): a full idempotent reconstruction of the
per-ET-day, per-PC registered-basis delivery record from the JLab Rucio
file inventory — the record the Snapper campaign view's curves draw
from. Logic in ``swf_epicprod/analytics/delivery_daily.py``; see
``swf-epicprod/docs/CAMPAIGN_DELIVERY.md`` (Ongoing production).
Django-bootstrap standalone script — also usable by hand.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/delivery-daily-rebuild.py [--dry-run]
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

from swf_epicprod.analytics.delivery_daily import rebuild_delivery_daily  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--campaigns', default='',
                        help='comma-separated campaign families '
                             '(default: all in the catalog)')
    parser.add_argument('--dry-run', action='store_true',
                        help='reconstruct and report without writing')
    parser.add_argument('--limit-files', type=int, default=0,
                        help='cap the metadata pass for fast validation '
                             '(implies --dry-run)')
    parser.add_argument('--created-by', default='prodops_agent')
    args = parser.parse_args()
    campaigns = tuple(c.strip() for c in args.campaigns.split(',')
                      if c.strip()) or None

    summary = rebuild_delivery_daily(
        campaigns, apply=not args.dry_run and not args.limit_files,
        created_by=args.created_by, limit_files=args.limit_files)
    print('SUMMARY ' + json.dumps(summary))

    if not args.dry_run and not args.limit_files:
        # The campaign view serves its series as a cached product;
        # rebuilding it here, right after the record changed, means
        # pages land on a warm product instead of churning behind a
        # stale one. A prewarm failure is reported, never fatal to
        # the chain step — the next page visit rebuilds behind.
        try:
            from snapper_ai.presentation import prewarm_focus_series
            warmed = prewarm_focus_series('epicprod',
                                          window_keys=('30d',))
            print('PREWARM ' + json.dumps(warmed))
        except Exception as exc:  # noqa: BLE001
            print(f'WARNING: series prewarm failed: {exc}',
                  file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
