#!/usr/bin/env python3
"""worker-record-capture.py — copy the harvester worker records of the
allocation page's queues before PanDA drops them.

The prod-ops agent's doer for ``worker_record_capture``, nightly by cron
enqueue: every worker row at the NERSC queues updated in the window is
copied into our store (monitor_app.worker_records), so an allocation keeps
its batch start, end and cores after PanDA's three-month window
(swf-epicprod docs/EPICPROD_OPS.md, Harvester worker records).

Django-bootstrap standalone script, also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/worker-record-capture.py [--days 100]
"""
import argparse
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from monitor_app import worker_records  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=float, default=worker_records.DEFAULT_DAYS,
                        help='window of worker updates to copy (default 100, all PanDA holds)')
    args = parser.parse_args()
    counts = worker_records.capture(days=args.days)
    print(f"read={counts['read']} created={counts['created']} updated={counts['updated']} "
          f"unchanged={counts['unchanged']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
