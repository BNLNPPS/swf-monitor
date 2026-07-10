#!/usr/bin/env python3
"""epic-prod-past-import.py — clockwork past-campaign output ingest.

The prod-ops agent's doer for the nightly ``catalog_sync`` chain step
(also behind the Past tab's "Update from epic-prod" button flow and
usable by hand): pull the cloned eic/epic-prod bookkeeping repo, then
re-run the idempotent FULL/RECO past-campaign ingest so every campaign's
recorded production content tracks what the production team publishes —
no button required. The chain runs before the general 04:00 repo pull,
so the pull here is what makes the ingest read today's upstream state.
See ``docs/EPICPROD_DATA_LINEAGE.md`` and ``docs/PCS.md``.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/epic-prod-past-import.py
"""
import argparse
import os
import subprocess
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from pcs.services import EPIC_PROD_PATH, import_epic_prod_past_campaigns  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--created-by', default='prodops_agent')
    parser.add_argument('--no-pull', action='store_true',
                        help='skip the git pull of the epic-prod clone')
    args = parser.parse_args()

    if not args.no_pull:
        pull = subprocess.run(
            ['git', '-C', EPIC_PROD_PATH, 'pull', '--ff-only'],
            capture_output=True, text=True, timeout=120)
        if pull.returncode != 0:
            # A failed pull is reported but not fatal: the ingest still
            # runs over the existing clone rather than silently skipping.
            print(f'WARNING: epic-prod pull failed '
                  f'(rc={pull.returncode}): {(pull.stderr or "").strip()}',
                  file=sys.stderr)
        else:
            print(f'epic-prod pull: {(pull.stdout or "").strip().splitlines()[-1]}')

    summary = import_epic_prod_past_campaigns(created_by=args.created_by)
    print(f"created={summary['created']} updated={summary['updated']} "
          f"errors={len(summary['errors'])}")
    for err in summary['errors']:
        print(f'ERROR: {err}', file=sys.stderr)
    return 1 if summary['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
