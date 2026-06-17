#!/usr/bin/env python
"""Import ePIC production-request questionnaire CSV into PCS.

Standalone Django-bootstrap script, usable by hand or cron. It imports the
Google Form responses-sheet CSV export through the same PCS service used by
the web import button.

Usage:
    cd /data/wenauseic/github/swf-monitor
    source ../swf-testbed/.venv/bin/activate && source ~/.env
    scripts/import-questionnaires.py --url 'https://docs.google.com/.../export?format=csv'
    scripts/import-questionnaires.py --file responses.csv
"""
import argparse
import os
import sys
from urllib.request import urlopen


THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()


def _read_input(args):
    if args.url:
        with urlopen(args.url, timeout=args.timeout) as response:
            return response.read().decode('utf-8-sig'), args.url
    if args.file:
        with open(args.file, 'r', encoding='utf-8-sig') as f:
            return f.read(), args.file
    raise ValueError('provide --url or --file')


def main(argv=None):
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--url', help='Link-readable Google Sheet CSV export URL')
    group.add_argument('--file', help='Local CSV file')
    parser.add_argument('--created-by', default='questionnaire_import')
    parser.add_argument('--timeout', type=int, default=30)
    args = parser.parse_args(argv)

    from pcs.services import questionnaire_intake_csv, ServiceError

    try:
        csv_text, source_url = _read_input(args)
        summary = questionnaire_intake_csv(
            csv_text, source_url=source_url, created_by=args.created_by)
    except (OSError, ValueError, ServiceError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1

    print(
        f"questionnaires: {summary['created']} new, "
        f"{summary['updated']} updated, {summary['unchanged']} unchanged"
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
