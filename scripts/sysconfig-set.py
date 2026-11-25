#!/usr/bin/env python3
"""Set SysConfig keys and record the edit in the action stream.

Merges KEY=VALUE pairs into the SysConfig document (VALUE is read as
JSON, so 90 is a number and "x" a string; a value that is not JSON is
taken as a string) and records one sysconfig_edit action carrying the
keys, their old and new values, who and why. The System page's editor
replaces the whole document; this changes only the keys named, so an
edit made elsewhere in the meantime is kept. SysConfig and its
conventions: docs/ACTION_STREAM.md, SysConfig.

By hand, from swf-monitor/src:
    ../../swf-testbed/.venv/bin/python ../scripts/sysconfig-set.py \\
        harvester_stdout.keep_days=90 --by wenauseic --reason "..." [--dry-run]
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
from monitor_app.models import SysConfig  # noqa: E402

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format='%(levelname)s sysconfig-set %(message)s')
log = logging.getLogger(__name__)


def parse_pair(text):
    if '=' not in text:
        raise ValueError(f'not KEY=VALUE: {text!r}')
    key, raw = text.split('=', 1)
    key = key.strip()
    if not key:
        raise ValueError(f'empty key: {text!r}')
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    return key, value


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('pairs', nargs='+', metavar='KEY=VALUE')
    parser.add_argument('--by', required=True,
                        help='who the edit is recorded for')
    parser.add_argument('--reason', required=True,
                        help='why, one short clause')
    parser.add_argument('--dry-run', action='store_true',
                        help='show the change and write nothing')
    args = parser.parse_args()

    try:
        updates = dict(parse_pair(p) for p in args.pairs)
    except ValueError as exc:
        log.error('%s', exc)
        print(json.dumps({'ok': False, 'error': str(exc)}))
        return 1

    current = SysConfig.get_config()
    changes = {k: {'old': current.get(k), 'new': v}
               for k, v in updates.items()}
    for key, change in changes.items():
        log.info('%s: %r -> %r', key, change['old'], change['new'])
    if args.dry_run:
        print(json.dumps({'ok': True, 'dry_run': True, 'changes': changes}))
        return 0

    summary = '; '.join(f"{k} {c['old']!r} -> {c['new']!r}"
                        for k, c in changes.items())
    try:
        SysConfig.update_config(updates, username=args.by)
    except Exception as exc:
        log.error('update failed: %s', exc)
        log_epicprod_action(
            'web', 'sysconfig_edit', username=args.by, outcome='error',
            level=logging.ERROR, reason=str(exc), keys=sorted(updates),
            message=f'sysconfig_edit error — {summary}: {exc}')
        print(json.dumps({'ok': False, 'error': str(exc)}))
        return 1
    log_epicprod_action(
        'web', 'sysconfig_edit', username=args.by,
        keys=sorted(updates), changes=changes, summary=args.reason,
        message=f'sysconfig_edit ok — {summary} ({args.reason})')
    print(json.dumps({'ok': True, 'changes': changes}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
