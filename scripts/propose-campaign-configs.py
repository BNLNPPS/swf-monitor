#!/usr/bin/env python3
"""propose-campaign-configs.py — the campaign configuration proposer:
for every campaign edition with tasks and no Standard Production
configuration, propose the ping and its remedy through the AI proposal
subsystem (swf-monitor docs/PINGS.md § Pings with a remedy,
docs/AI_PROPOSALS.md category standard_config). Logic in
``pcs.config_proposer``; the prod-ops agent's doer for the
``campaign_config_propose`` chain step, also runnable by hand.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/propose-campaign-configs.py [--apply] [--created-by X]

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

from pcs.config_proposer import propose_campaign_configs  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true',
                        help='submit the proposals (default: report only)')
    parser.add_argument('--created-by', default='prodops_agent')
    args = parser.parse_args()
    result = propose_campaign_configs(created_by=args.created_by,
                                      apply=args.apply)
    for f in result['findings']:
        print(f"finding: {f['edition']} ({f['campaign']}, {f['lifecycle']}), "
              f"{f['tasks']} tasks, no Standard Production configuration")
    if not result['findings']:
        print('no edition lacks its Standard Production configuration')
    summary = {
        'findings': len(result['findings']),
        'editions': [f['edition'] for f in result['findings']],
        'applied': bool(args.apply),
        'pings': result['pings'], 'remedies': result['remedies'],
        'withdrawn': result['withdrawn'],
        'fulfil_proposed': result['fulfil_proposed'],
    }
    print('SUMMARY ' + json.dumps(summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
