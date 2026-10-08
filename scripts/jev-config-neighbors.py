#!/usr/bin/env python3
"""jev-config-neighbors.py — configurations like this one: for each
physics configuration, its nearest configurations in physics, ranked by
Jev (TypeSafe AI) on one rubric (swf-epicprod docs/JEV.md, Neighbours;
logic in swf_epicprod/config_neighbors.py).

The prod-ops agent's doer for ``jev_config_neighbors``: all
configurations nightly by cron enqueue, or the configurations named in
the message. Stores the cached product ``jev_config_neighbors`` and
records one ``jev_config_neighbors`` action. Django-bootstrap
standalone script, also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/jev-config-neighbors.py [--label pc434 ...] [--created-by NAME]

Prints one ``SUMMARY`` JSON line; exit 1 when nothing was computed.
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

from swf_epicprod.config_neighbors import run  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--label', action='append', default=[],
                    help='a configuration to compute (repeatable); all when absent')
    ap.add_argument('--created-by', default='jev_config_neighbors')
    args = ap.parse_args()
    summary = run(labels=args.label or None, created_by=args.created_by)
    print('SUMMARY ' + json.dumps(summary, default=str))
    return 0 if summary.get('computed') else 1


if __name__ == '__main__':
    sys.exit(main())
