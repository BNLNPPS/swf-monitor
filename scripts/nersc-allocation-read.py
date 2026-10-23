#!/usr/bin/env python3
"""nersc-allocation-read.py — read the NERSC allocation balance of the
projects production charges, from the NERSC IRI API with the access
token file a PanDA operations cron refreshes daily (swf-epicprod
docs/CONTINUOUS_PRODUCTION.md, Placement, Closed queues; logic in
swf_epicprod/nersc_allocation.py).

The prod-ops agent's doer for ``nersc_allocation_read``, hourly by cron
enqueue. Stores the cached product ``nersc_allocation`` and records one
``nersc_allocation_read`` action. Django-bootstrap standalone script,
also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/nersc-allocation-read.py [--created-by NAME]

Prints the balance lines and one ``SUMMARY`` JSON line; exit 1 when the
balance could not be read.
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

from swf_epicprod.nersc_allocation import line, run  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--created-by', default='nersc_allocation',
                    help='who runs the read (the record carries it)')
    args = ap.parse_args()
    record = run(created_by=args.created_by)
    for text in line(record):
        print(text)
    print('SUMMARY ' + json.dumps({k: v for k, v in record.items() if k != 'token_file'},
                                  default=str))
    return 1 if record.get('error') else 0


if __name__ == '__main__':
    sys.exit(main())
