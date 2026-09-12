#!/usr/bin/env python3
"""segfault-findings-serials.py — permanent ids for the findings written
before serials existed.

A finding draws its serial (data['serial'], shown as f-n) once, when it is
written (monitor_app/segfaults.py, set_finding). The findings written
before 2026-09-12 have none; this assigns them in creation order and
prints what it assigned. Idempotent: a finding with a serial is left as
it is.

Django-bootstrap standalone script, by hand::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/segfault-findings-serials.py --by wenaus
"""
import argparse
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from monitor_app.segfaults import assign_finding_serials, finding_id  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--by', default='operator', help='the version stamp on each finding written')
    args = ap.parse_args()
    assigned = assign_finding_serials(args.by)
    for name, serial in assigned:
        print(f'{finding_id(serial)}  {name}')
    print(f'{len(assigned)} finding(s) given a serial')
    return 0


if __name__ == '__main__':
    sys.exit(main())
