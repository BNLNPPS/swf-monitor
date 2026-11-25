#!/usr/bin/env python3
"""Set a queue's scheduled canary probe and record the change in the action stream.

Enables a queue's probe with an interval (and, optionally, the PCS task
whose payload canary is its scheduled probe), or disables it: the same
write as the Save and Disable controls of the canary probes page,
through the page's own function (canary.store.views._apply_probe_config;
site-canary docs/IMPLEMENTATION.md, Probes and Payload canaries). The
page's JSON API takes a signed-in session only; this is the route for a
session that holds none. Records one canary_probe_config action with
the probe block before and after, who and why.

By hand, from swf-monitor/src:
    ../../swf-testbed/.venv/bin/python ../scripts/canary-probe-config.py \\
        QUEUE --disable --by wenauseic --reason "..." [--dry-run]
    ../../swf-testbed/.venv/bin/python ../scripts/canary-probe-config.py \\
        QUEUE --interval-hours 12 [--payload-task NAME] --by ... --reason ...
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

from canary.store.models import Queue  # noqa: E402
from canary.store.views import _apply_probe_config  # noqa: E402
from monitor_app.epicprod_logging import log_epicprod_action  # noqa: E402

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format='%(levelname)s canary-probe-config %(message)s')
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('queue')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--disable', action='store_true',
                      help='switch the scheduled probe off')
    mode.add_argument('--interval-hours', type=float,
                      help='enable the scheduled probe at this interval')
    parser.add_argument('--payload-task', default=None,
                        help='PCS task whose payload canary is the scheduled probe '
                             '(empty: a landing probe; omitted: unchanged)')
    parser.add_argument('--by', required=True,
                        help='who the change is recorded for')
    parser.add_argument('--reason', required=True,
                        help='why, one short clause')
    parser.add_argument('--dry-run', action='store_true',
                        help='show the change and write nothing')
    args = parser.parse_args()

    queue = Queue.objects.filter(name=args.queue).first()
    if queue is None:
        log.error('unknown queue %r', args.queue)
        print(json.dumps({'ok': False, 'error': f'unknown queue {args.queue!r}'}))
        return 1
    action = 'disable' if args.disable else 'save'
    before = (queue.data or {}).get('probe')
    log.info('%s: %s, probe block %r', queue.name, action, before)
    if args.dry_run:
        print(json.dumps({'ok': True, 'dry_run': True, 'queue': queue.name,
                          'action': action, 'before': before}))
        return 0

    error = _apply_probe_config(queue, action, args.interval_hours, args.payload_task)
    if error:
        log.error('refused: %s', error)
        log_epicprod_action(
            'web', 'canary_probe_config', username=args.by, outcome='error',
            level=logging.ERROR, queue=queue.name, reason=error,
            message=f'canary_probe_config error — {queue.name} {action}: {error}')
        print(json.dumps({'ok': False, 'error': error}))
        return 1
    after = queue.data.get('probe')
    log_epicprod_action(
        'web', 'canary_probe_config', username=args.by, queue=queue.name,
        before=before, after=after, summary=args.reason,
        message=f'canary_probe_config ok — {queue.name} {action} ({args.reason})')
    print(json.dumps({'ok': True, 'queue': queue.name, 'action': action,
                      'before': before, 'after': after}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
