#!/usr/bin/env python3
"""Take live records off the computing centre's epicprod-live screen.

Writes one ``live_cleared`` record to the epicprod action stream naming the
cleared log record ids and the reason (COMP_SNAPSHOT.md, Live). The comp
snapshot leaves the named records out of the screen; the records themselves
are unchanged on the logs page.

    source ~/.env
    python scripts/live-clear.py 6221565 6223208 --reason "door probe fixed 04:37"

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
    parser.add_argument('ids', nargs='+', type=int, help='log record ids')
    parser.add_argument('--reason', required=True,
                        help='why they are off the screen')
    parser.add_argument('--by', default=os.environ.get('USER', ''),
                        help='who cleared them (default: $USER)')
    args = parser.parse_args()

    base = os.environ.get('SWF_MONITOR_HTTP_URL', '').rstrip('/')
    token = os.environ.get('SWF_API_TOKEN', '')
    if not base or not token:
        sys.exit('SWF_MONITOR_HTTP_URL and SWF_API_TOKEN must be set '
                 '(source ~/.env)')

    ids = sorted(set(args.ids))
    summary = (f"cleared {len(ids)} record(s) from the epicprod-live screen: "
               f"{', '.join(str(i) for i in ids)}; {args.reason}")
    body = {
        'app_name': 'epicprod',
        'instance_name': 'live-clear',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'level': 20,
        'levelname': 'INFO',
        'message': summary,
        'module': 'live_clear',
        'funcname': 'main',
        'lineno': 0,
        'process': os.getpid(),
        'thread': 0,
        'extra_data': {
            'action': 'live_cleared',
            'outcome': 'ok',
            'sublevel': 'low',
            'live_default': False,
            'username': args.by,
            'cleared_ids': ids,
            'reason': args.reason,
            'summary': summary,
        },
    }
    request = urllib.request.Request(
        f'{base}/api/logs/', data=json.dumps(body).encode(), method='POST',
        headers={'Content-Type': 'application/json',
                 'Authorization': f'Token {token}'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            record = json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as exc:
        sys.exit(f'HTTP {exc.code} from {base}/api/logs/: '
                 f'{exc.read()[:500].decode(errors="replace")}')
    except urllib.error.URLError as exc:
        sys.exit(f'{base}/api/logs/ unreachable: {exc.reason}')
    print(f"live_cleared record {record.get('id', '?')}: {summary}")


if __name__ == '__main__':
    main()
