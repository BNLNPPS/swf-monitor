#!/usr/bin/env python3
"""jev-like.py — the configurations matching a request in plain words,
ranked by Jev (TypeSafe AI) (swf-epicprod docs/JEV.md, Plain-language
search; logic in swf_epicprod/jev_like.py).

The prod-ops agent's doer for ``jev_like``, asked from the find page.
Stores the cached product ``jev_like:<key>`` and records one
``jev_like`` action; the agent then publishes ``jev_like_ready``.
Django-bootstrap standalone script, also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/jev-like.py --text "18x275 radiative DIS Q2 above 10"

Prints one ``SUMMARY`` JSON line; exit 1 when Jev gave no answer.
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

from swf_epicprod.jev_like import run  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--text', required=True)
    ap.add_argument('--created-by', default='jev_like')
    ap.add_argument('--response-url', default='',
                    help='a Mattermost slash command response URL to post the answer to')
    args = ap.parse_args()
    value = run(args.text, created_by=args.created_by, response_url=args.response_url)
    print('SUMMARY ' + json.dumps({'key': value['key'], 'ranked': len(value['ranked']),
                                   'error': value['error']}))
    return 1 if value['error'] else 0


if __name__ == '__main__':
    sys.exit(main())
