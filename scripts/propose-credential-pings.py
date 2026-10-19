#!/usr/bin/env python3
"""propose-credential-pings.py — the credential expiry proposer: a ping
on the expiry of each production credential whose date can be read, and
a fulfilment proposal for an open ping whose credential has since been
renewed (swf-monitor docs/PINGS.md, docs/AI_PROPOSALS.md category
``ping``). Logic in ``swf_epicprod.credential_proposer``; the prod-ops
agent's doer for the ``credential_ping_propose`` chain step, also
runnable by hand.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/propose-credential-pings.py [--apply] [--created-by X]

Dry run by default: the findings are printed and nothing is proposed.
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

from swf_epicprod.credential_proposer import (  # noqa: E402
    propose_credential_pings)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true',
                        help='submit the proposals (default: report only)')
    parser.add_argument('--created-by', default='prodops_agent')
    args = parser.parse_args()
    result = propose_credential_pings(created_by=args.created_by,
                                      apply=args.apply)
    for f in result['findings']:
        print(f"finding: {f['label']} expires {f['due']} "
              f"({f['days_left']:.1f} days left, {f['status']})")
    if not result['findings']:
        print('no credential expiry could be read')
    pings = result['pings'] or {}
    summary = {
        'findings': len(result['findings']),
        'applied': bool(args.apply),
        'proposed': pings.get('proposed', []),
        'noop': pings.get('noop', []),
        'denied': pings.get('denied', []),
        'invalid': pings.get('invalid', []),
        'fulfil_proposed': result['fulfil_proposed'],
        'withdrawn': result['withdrawn'],
        'errors': result['errors'],
    }
    print('SUMMARY ' + json.dumps(summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
