#!/usr/bin/env python3
"""rucio-arrivals-sweep.py — detect new files landing in JLab Rucio.

The prod-ops agent's doer for the clockwork arrivals sweep (a step of the
nightly ``catalog_sync`` chain): one ``created_after`` DID query per root
across all campaign versions, per-campaign arrivals recorded on the
Campaign rows, one live ``rucio_arrivals`` event when anything arrived.
Django-bootstrap standalone script — also usable by hand. See
``docs/EPICPROD_DATA_LINEAGE.md``.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/rucio-arrivals-sweep.py [--window-hours 24]
"""
import argparse
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from pcs.services import sweep_rucio_arrivals  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--window-hours', type=float, default=None,
                        help='override the since-last-sweep window')
    parser.add_argument('--created-by', default='prodops_agent')
    args = parser.parse_args()

    result = sweep_rucio_arrivals(
        window_hours=args.window_hours,
        created_by=args.created_by,
        instance='catalog-sync',
    )
    print(f"total_files={result['total_files']} "
          f"window_start={result['window_start']}")
    for name, files in sorted(result['campaigns'].items()):
        marker = '' if name in result['known'] else '  [no catalog campaign]'
        print(f'  {name}: {files} file(s){marker}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
