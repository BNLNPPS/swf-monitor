#!/usr/bin/env python3
"""sysconfig-set.py — set one SysConfig key, the single-key counterpart
of the System page editor (monitor_app.viewdir.system_status.sysconfig_save),
recorded the same way: one ``sysconfig_edit`` action-stream event carrying
the key, the previous and new values, the editor and the reason.

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/sysconfig-set.py front.jedi_throttled false \
        --by wenaus --comment "the JEDI throttler observes; the front keeps its own caps"

The value is parsed as JSON (``false``, ``8.0``, ``"active"``, ``[...]``);
a value that is not JSON is stored as a string. ``--dry-run`` prints the
change and writes nothing. Exit 1 on failure.
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

from monitor_app.epicprod_logging import log_epicprod_action  # noqa: E402
from monitor_app.models import SysConfig  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('key')
    parser.add_argument('value')
    parser.add_argument('--by', required=True, help='the editor recorded on the change')
    parser.add_argument('--comment', required=True, help='why the key changes')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    try:
        value = json.loads(args.value)
    except ValueError:
        value = args.value
    config = SysConfig.get_config()
    present = args.key in config
    previous = config.get(args.key)
    print(json.dumps({'key': args.key, 'present': present, 'previous': previous,
                      'value': value, 'by': args.by, 'dry_run': args.dry_run}))
    if args.dry_run:
        return 0
    SysConfig.update_config({args.key: value}, username=args.by)
    log_epicprod_action(
        'web', 'sysconfig_edit', username=args.by,
        sublevel='high', live_default=True,
        keys=[args.key], key=args.key, previous=previous, value=value,
        comment=args.comment,
        message=f'sysconfig_edit {args.key}: {previous!r} -> {value!r}: {args.comment}',
    )
    after = SysConfig.get_config().get(args.key)
    if after != value:
        print(f'ERROR: {args.key} reads {after!r} after the write', file=sys.stderr)
        return 1
    print(f'{args.key} = {after!r}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
