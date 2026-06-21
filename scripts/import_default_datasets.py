#!/usr/bin/env python3
"""
import_default_datasets.py — idempotent import of Sakib's default-datasets CSV
into the PCS Production Task Catalog.

Source: ``eic/epic-prod/docs/_data/datasets.csv``. Each CSV row becomes
one Dataset and one ProdTask (status=``csv_import``) linked to the
current Campaign. Re-running updates rows in place (idempotency key is
``(Dataset Path, Generator/Dataset Version)``).

The same logic backs the "Update from CSV" button on the catalog page;
this script exists so an operator can run the import from the shell
(or a cron job) without going through the web UI.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/import_default_datasets.py                 # default path
    python ../scripts/import_default_datasets.py path/to/other.csv
"""
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from pcs.services import (  # noqa: E402
    import_default_datasets_csv,
    DEFAULT_DATASETS_CSV_PATH,
    ServiceError,
)


def main(argv):
    csv_path = argv[1] if len(argv) > 1 else DEFAULT_DATASETS_CSV_PATH
    print(f'Importing from: {csv_path}')
    try:
        summary = import_default_datasets_csv(csv_path)
    except (ServiceError, FileNotFoundError, OSError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1
    print(f'  rows:    {summary["rows"]}')
    print(f'  created: {summary["created"]}')
    print(f'  updated: {summary["updated"]}')
    for action, n in sorted(summary.get('tag_actions', {}).items()):
        print(f'  physics tag {action}: {n} rows')
    if summary['errors']:
        print(f'  errors:  {len(summary["errors"])}')
        for err in summary['errors'][:10]:
            print(f'    - {err}')
        if len(summary['errors']) > 10:
            print(f'    ... and {len(summary["errors"]) - 10} more')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
