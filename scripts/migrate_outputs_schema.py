#!/usr/bin/env python3
"""migrate_outputs_schema.py — one-time migration of produced-output data onto
the unified ``ProdTask.overrides['outputs']`` schema.

Reshapes each ``past_output`` block into one ``outputs`` entry (one per
produced Rucio dataset) and drops the superseded ``csv_import.output``
aggregate. Idempotent (skips tasks that already carry ``outputs``). Dry run by
default. See ``swf-monitor/docs/EPICPROD_DATA_LINEAGE.md``.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/migrate_outputs_schema.py            # dry run
    python ../scripts/migrate_outputs_schema.py --apply    # write
"""
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from pcs.services import migrate_outputs_schema  # noqa: E402


def main(argv):
    apply = '--apply' in argv[1:]
    print('APPLY — writing changes' if apply
          else 'DRY RUN — no writes (pass --apply to write)')
    summary = migrate_outputs_schema(apply=apply)
    print(f'  tasks seen:            {summary["seen"]}')
    print(f'  past_output migrated:  {summary["past_migrated"]}')
    print(f'  csv aggregate dropped: {summary["aggregate_dropped"]}')
    if summary['errors']:
        print(f'  errors:  {len(summary["errors"])}', file=sys.stderr)
        for err in summary['errors'][:10]:
            print(f'    - {err}', file=sys.stderr)
        if len(summary['errors']) > 10:
            print(f'    ... and {len(summary["errors"]) - 10} more', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
