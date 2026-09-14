#!/usr/bin/env python3
"""Opt-in, root-owned TeamComms claim guard for the existing deployment script."""
import argparse
import os
from pathlib import Path
from uuid import UUID

CONFIG = Path('/opt/swf-monitor/config/teamcomms')
LIVE = Path('/opt/swf-monitor/current')
CHECKOUT = Path('/data/wenauseic/github/swf-monitor')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--claim-id', required=True, type=UUID)
    parser.add_argument('--generation', required=True, type=int)
    parser.add_argument('--check', action='store_true', help='Validate guarded admission and report the committed checkout; no deployment')
    parser.add_argument('kind', choices=['branch', 'tag'], nargs='?')
    parser.add_argument('reference', nargs='?')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run this wrapper itself with sudo so the guard can stop the entire privileged process group')
    if args.generation < 1 or (not args.check and (not args.kind or not args.reference)) or (args.check and (args.kind or args.reference)):
        parser.error('Choose --check or exactly one branch/tag reference, with a positive generation')
    # Resolve current once: cutover must not change the Python/script mid-run.
    release = LIVE.resolve(strict=True)
    from teamcomms.connectors.guard import load_guard
    guard = load_guard(CONFIG / 'guard.json')
    command = [str(release / '.venv/bin/python'), '-m', 'teamcomms.connectors.cli',
               '--config', str(CONFIG / 'connector.json'), 'guard', '--guard-config', str(CONFIG / 'guard.json'),
               '--claim-id', str(args.claim_id), '--generation', str(args.generation)]
    for resource in guard.resource_ids:
        command.extend(['--resource', str(resource)])
    if args.check:
        target = ['/usr/bin/git', '-c', 'safe.directory=' + str(CHECKOUT), '-C', str(CHECKOUT), 'show', '-s', '--format=%H %s', 'HEAD']
    else:
        # This committed release script retains its standard drift/frozen-tree checks.
        target = ['/bin/bash', str(release / 'deploy-swf-monitor.sh'), args.kind, args.reference]
    os.execv(command[0], command + ['--'] + target)


if __name__ == '__main__':
    main()
