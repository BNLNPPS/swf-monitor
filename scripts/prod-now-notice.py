#!/usr/bin/env python3
"""Put an informational line on Production now for a number of hours.

Writes one ``prod_now_notice`` record to the epicprod action stream: the
line's text, the hours it stands, and the page it links to. The comp
snapshot shows it grey among the Production now lines until its hours
have passed (COMP_SNAPSHOT.md, Report and errors). The text before its
first ": " names what the line is about, as every Production now line's
does.

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/prod-now-notice.py \\
        "Campaign 26.07: over a billion events delivered" --hours 24 \\
        --link "https://epic-devcloud.org/prod/pcs/plan/?campaign=26.07" --by wenaus
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

from monitor_app.epicprod_logging import log_epicprod_action  # noqa: E402


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('text', help='the line, "<what it is about>: <what holds>"')
    parser.add_argument('--hours', type=float, required=True,
                        help='how long the line stands')
    parser.add_argument('--link', default='', help='the page the line opens')
    parser.add_argument('--by', required=True, help='the person putting it up')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the record; write nothing')
    args = parser.parse_args()

    text = ' '.join(args.text.split())
    if not text or args.hours <= 0:
        logging.error('a line needs text and a positive number of hours')
        print(json.dumps({'ok': False, 'error': 'text and positive hours required'}))
        return 1
    fields = {'title': text, 'summary': text, 'hours': args.hours,
              'url': args.link, 'subject_type': 'prod_now',
              'subject_label': text.split(': ', 1)[0]}
    if args.dry_run:
        print(json.dumps({'ok': True, 'dry_run': True, **fields}))
        return 0
    log_id = log_epicprod_action(
        'prod-now', 'prod_now_notice', username=args.by,
        message=f'Production now: {text} ({args.hours:g} h)', **fields)
    if not log_id:
        logging.error('the prod_now_notice record was not written')
        print(json.dumps({'ok': False, 'error': 'record not written'}))
        return 1
    print(json.dumps({'ok': True, 'log_id': log_id, **fields}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
