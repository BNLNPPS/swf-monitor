#!/usr/bin/env python3
"""batch-log-learn.py — mine the batch-record corpus before it ages out.

The prod-ops agent's doer for the learning pass (docs/ERROR_ATTRIBUTION.md,
Retention and learning). It reads every captured condor event log and
writes the knowledge base beside them: the catalogue of event codes that
actually occur with counts and an example each, the taxonomy of recurring
reasons with the jobs and days they appear on, and the lines that are
boilerplate because they are in most logs. It then rescues one raw log per
pattern, and every log matching no pattern, under ``keep``, so retention
can delete a date directory whole without deciding anything about content.

The parser improves from what the corpus contains rather than from an
author's expectation of it, and every entry in the knowledge base keeps the
raw evidence behind it.

Django-bootstrap standalone script — also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/batch-log-learn.py [--no-rescue]

The last stdout line is a JSON summary; the readable report is above it.
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

from monitor_app import batch_records  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-rescue', action='store_true',
                        help='build the knowledge base, rescue nothing')
    parser.add_argument('--root', default=None, help='override the store root')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args()

    knowledge = batch_records.learn(root=args.root, rescue=not args.no_rescue)

    if not args.quiet:
        print(f"logs read: {knowledge['logs']}")
        print(f"event codes: {len(knowledge['codes'])}, "
              f"unnamed by the parser: {', '.join(knowledge['unnamed_codes']) or 'none'}")
        for code in knowledge['codes'][:12]:
            name = code['name'] or f"event {code['code']} (unnamed)"
            print(f"  {code['code']}  {name:<24} {code['count']:5d} in {code['logs']} logs")
        print(f"reason patterns: {len(knowledge['patterns'])}")
        for pattern in knowledge['patterns'][:12]:
            print(f"  {pattern['count']:5d}  {pattern['event']:<18} "
                  f"{pattern['example'][:100]}")
        print(f"boilerplate lines: {len(knowledge['boilerplate'])}")
        if 'rescued' in knowledge:
            print(f"rescued: {knowledge['rescued']}")

    print(json.dumps({
        'logs': knowledge['logs'],
        'codes': len(knowledge['codes']),
        'unnamed_codes': knowledge['unnamed_codes'],
        'patterns': len(knowledge['patterns']),
        'boilerplate': len(knowledge['boilerplate']),
        'rescued': knowledge.get('rescued', {}),
    }))
    return 0


if __name__ == '__main__':
    sys.exit(main())
