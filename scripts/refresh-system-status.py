#!/usr/bin/env python3
"""Refresh cached System status rows.

Standalone doer for the epicprod ops agent. This is intentionally not a
Django management command: the web page reads cached rows; the agent runs this
script for manual and periodic refreshes.
"""

import argparse
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from monitor_app.system_status import compact_refresh_report, refresh_system_status  # noqa: E402
from monitor_app.snapper_health import (  # noqa: E402
    compact_publication_report,
    publish_health_components,
)


def main(argv):
    ap = argparse.ArgumentParser(description='Refresh cached SWF monitor system status.')
    ap.add_argument('--source', default='manual', help='Refresh source label stored in JSON data.')
    ap.add_argument('--only', action='append', default=[],
                    help='Collector name to refresh. Repeat for multiple collectors.')
    args = ap.parse_args(argv[1:])

    rows = refresh_system_status(selected=args.only or None, source=args.source)
    publications = publish_health_components()
    print(compact_refresh_report(rows))
    print(compact_publication_report(publications))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
