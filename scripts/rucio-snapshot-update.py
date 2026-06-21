#!/usr/bin/env python3
"""rucio-snapshot-update.py — refresh the JLab Rucio output snapshot for the
current (+ last) PCS campaign and rematch produced datasets onto each task's
``overrides['outputs']``.

This is the prod-ops agent's doer for the catalog "Update from Rucio" button:
the web view publishes ``rucio_snapshot_update`` to the agent, which runs this
script in the background (off the WSGI request, which times out on the live
JLab fetch) and pushes ``rucio_snapshot_ready`` to the browser when done.
Django-bootstrap standalone script — also usable by hand or cron. See
``docs/EPICPROD_DATA_LINEAGE.md`` and ``docs/EPICPROD_OPS.md``.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/rucio-snapshot-update.py
"""
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from pcs.services import refresh_rucio_snapshots  # noqa: E402


def main(argv):
    result = refresh_rucio_snapshots(created_by='prodops_agent')
    for s in result['summaries']:
        paths = ', '.join(f'{k}={v}' for k, v in s.get('paths', {}).items())
        match = f" match={s['match']}" if s.get('match') else ''
        print(f"  {s.get('campaign', '?')}: {paths}{match}")
    if result['errors']:
        for err in result['errors']:
            print(f'ERROR: {err}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
