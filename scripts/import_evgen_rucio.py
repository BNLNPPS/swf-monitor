#!/usr/bin/env python3
"""import_evgen_rucio.py — assimilate the JLab Rucio EVGEN inventory into PCS.

Fetches ``epic:/EVGEN/*`` from JLab Rucio, saves a snapshot, and resolves each
PCS evgen Dataset to the Rucio EVGEN dataset(s) that realize it — matching on
the shared filter axes (beam, physics, Q² overlap), never on path strings.
One abstract request (``minQ2=N``) can resolve to several Q²-range datasets.
Re-running picks up a grown Rucio listing in the same way.

Dry run by default (no DB writes); pass ``--apply`` to persist
``metadata['rucio']`` onto the matched Datasets.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/import_evgen_rucio.py            # dry run
    python ../scripts/import_evgen_rucio.py --apply    # persist
"""
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from pcs.services import refresh_evgen_rucio, ServiceError  # noqa: E402


def main(argv):
    apply = '--apply' in argv[1:]
    print('EVGEN Rucio assimilation' + (' (APPLY)' if apply else ' (dry run)'))
    try:
        s = refresh_evgen_rucio(apply=apply)
    except ServiceError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1
    print(f"  rucio EVGEN datasets:   {s['rucio_evgen']}")
    print(f"  PCS evgen datasets:     {s['datasets_seen']}")
    print(f"  matched:                {s['datasets_matched']}")
    print(f"  unmatched (PCS):        {s['datasets_unmatched']}")
    print(f"  unmatched (Rucio):      {s['rucio_unmatched']}")
    print(f"  snapshot:               {s['snapshot_path']}")
    for ex in s.get('samples', []):
        print(f"    {ex['request']}")
        for d in ex['matched']:
            print(f"        -> {d}")
    if s['errors']:
        print(f"  errors: {len(s['errors'])}")
        for e in s['errors'][:10]:
            print(f"    - {e}")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
