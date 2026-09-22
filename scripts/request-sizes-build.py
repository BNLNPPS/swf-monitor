#!/usr/bin/env python3
"""request-sizes-build.py — build the record the Request size plot page
renders: how large the ePIC production requests are.

Every request record that states a number of events is read (a form
answer in the requester's own words, or a recorded count), the
distribution computed, and both stored as the cached product
``request_sizes:v1`` (swf-epicprod ``swf_epicprod/request_events.py``,
docs/REQUEST_SIZES.md). The page reads that row and never builds, so a
page render costs a row read.

The prod-ops agent's doer for ``request_sizes_build``, nightly. Requests
arrive by the day, so nothing is lost by building once a night.
Django-bootstrap standalone script, also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/request-sizes-build.py [--dry-run]

``--dry-run`` reads and prints, and stores nothing.
Prints one ``SUMMARY`` JSON line; exit 1 on failure.
"""
import argparse
import json
import logging
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

KEY = 'request_sizes:v1'
TTL_S = 7 * 24 * 3600      # the page shows what was built; staleness is visible


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--dry-run', action='store_true',
                    help='read and print, store nothing')
    ap.add_argument('--created-by', default='request-sizes',
                    help='who ran the build (the record carries it)')
    args = ap.parse_args()

    from django.utils import timezone
    from monitor_app.cached_product import get_product
    from monitor_app.epicprod_logging import log_epicprod_action
    from swf_epicprod.request_events import build, human

    started = timezone.now()
    try:
        state = build()
    except Exception as exc:  # noqa: BLE001
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    summary = state['summary']
    duration_s = (timezone.now() - started).total_seconds()

    if not args.dry_run:
        try:
            get_product(KEY, lambda: state, ttl_seconds=TTL_S, refresh=True)
        except Exception as exc:  # noqa: BLE001
            print(f'ERROR: storing {KEY}: {type(exc).__name__}: {exc}', file=sys.stderr)
            return 1
        log_epicprod_action(
            'request-sizes', 'request_sizes_build', username=args.created_by,
            outcome='ok', duration_ms=int(duration_s * 1000),
            sublevel='low', live_default=False, level=logging.INFO,
            message=(f'request sizes built: {summary["stated"]} of '
                     f'{summary["requests"]} requests state a count, median '
                     f'{human(summary["percentiles"].get("50"))}, '
                     f'p90 {human(summary["percentiles"].get("90"))}'),
            requests=summary['requests'], stated=summary['stated'],
            unstated=summary['unstated'], total_events=summary['total'])

    print(f"requests {summary['requests']}, stating a count {summary['stated']}, "
          f"median {human(summary['percentiles'].get('50'))}, "
          f"p90 {human(summary['percentiles'].get('90'))}, "
          f"max {human(summary['max'])}")
    print('SUMMARY ' + json.dumps(
        {k: v for k, v in summary.items()
         if k not in ('bins', 'cumulative', 'thresholds', 'apart')},
        default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
