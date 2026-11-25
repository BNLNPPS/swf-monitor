#!/usr/bin/env python3
"""Post a production finding or its resolution to epicprod-live.

Writes one ``production_finding`` record to the epicprod action stream
(importance high, live), which the epicprod-live subscription publishes and the
room's screen and the assessments read (docs/NOTICE_ROUTING.md, Findings and
resolutions). A resolution names the record it resolves with --resolves. Title
and detail are brief: what happened and to what in under ten words, then one
or two short sentences; evidence goes in --evidence links and record ids.

    source ~/.env
    python scripts/post-finding.py --title "Harvester's 421 vanished OSG pilots cleared" \\
        --detail "..." --subject-type panda_queue --subject-key BNL_OSG_PanDA_1 \\
        --resolves 7656686 --evidence https://... [--dry-run]

Environment: SWF_MONITOR_HTTP_URL (the monitor base URL) and SWF_API_TOKEN.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--title', required=True, help='what happened and to what, under ten words')
    parser.add_argument('--detail', required=True, help='one or two short sentences')
    parser.add_argument('--subject-type', default='', help='e.g. panda_queue, panda_task')
    parser.add_argument('--subject-key', default='', help='e.g. the queue name or JEDI task id')
    parser.add_argument('--resolves', type=int, help='the log record id of the finding this resolves')
    parser.add_argument('--evidence', nargs='*', default=[], help='evidence URLs')
    parser.add_argument('--by', default=os.environ.get('USER', ''), help='who reports (default: $USER)')
    parser.add_argument('--dry-run', action='store_true', help='print the record, post nothing')
    args = parser.parse_args()
    if len(args.title.split()) >= 10:
        sys.exit('The title must be under ten words (NOTICE_ROUTING.md, Findings and resolutions)')

    extra = {
        'action': 'production_finding', 'outcome': 'ok', 'sublevel': 'high', 'live_default': True,
        'username': args.by, 'title': args.title, 'summary': args.detail,
        'subject_type': args.subject_type, 'subject_key': args.subject_key,
    }
    if args.resolves:
        extra['resolves'] = args.resolves
    if args.evidence:
        extra['evidence'] = args.evidence
    body = {
        'app_name': 'epicprod', 'instance_name': 'post-finding',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'level': 20, 'levelname': 'INFO', 'message': args.title,
        'module': 'post_finding', 'funcname': 'main', 'lineno': 0,
        'process': os.getpid(), 'thread': 0, 'extra_data': extra,
    }
    if args.dry_run:
        print(json.dumps({'dry_run': True, 'record': body}))
        return

    base = os.environ.get('SWF_MONITOR_HTTP_URL', '').rstrip('/')
    token = os.environ.get('SWF_API_TOKEN', '')
    if not base or not token:
        sys.exit('SWF_MONITOR_HTTP_URL and SWF_API_TOKEN must be set (source ~/.env)')
    request = urllib.request.Request(
        f'{base}/api/logs/', data=json.dumps(body).encode(), method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': f'Token {token}'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            record = json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as exc:
        sys.exit(f'HTTP {exc.code} from {base}/api/logs/: {exc.read()[:500].decode(errors="replace")}')
    except urllib.error.URLError as exc:
        sys.exit(f'{base}/api/logs/ unreachable: {exc.reason}')
    print(json.dumps({'record': record.get('id'), 'title': args.title}))


if __name__ == '__main__':
    main()
