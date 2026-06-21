#!/usr/bin/env python3
"""pcs-catalog-import.py — run a PCS catalog import in the background.

The prod-ops agent's doer for the catalog 'Update from CSV' and 'Update from
epic-prod' buttons: the web view publishes ``catalog_import`` to the agent,
which runs this script off the WSGI request (the epic-prod walk of ~4900
datasets times the gateway out) and pushes ``catalog_import_ready`` to the
browser when done. Django-bootstrap standalone — also usable by hand or cron.
See ``docs/EPICPROD_OPS_AGENT.md`` and ``docs/SSE_PUSH.md``.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/pcs-catalog-import.py {csv|epic-prod}
"""
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()


def main(argv):
    source = argv[1] if len(argv) > 1 else ''
    from pcs.services import (import_default_datasets_csv,
                              import_epic_prod_past_campaigns, ServiceError)
    try:
        if source == 'csv':
            s = import_default_datasets_csv(created_by='prodops_agent')
            print(f"csv: {s['created']} new, {s['updated']} updated, "
                  f"{len(s['errors'])} errors (of {s['rows']} rows)")
        elif source == 'epic-prod':
            s = import_epic_prod_past_campaigns(created_by='prodops_agent')
            print(f"epic-prod: {s['created']} new, {s['updated']} updated, across "
                  f"{s['campaigns']} campaigns, {len(s['errors'])} errors "
                  f"(of {s['rows']} rows)")
        else:
            print(f"unknown source {source!r}; expected 'csv' or 'epic-prod'",
                  file=sys.stderr)
            return 2
    except (ServiceError, FileNotFoundError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    # Per-row warnings are not failures — surface them, but the import succeeded.
    for err in s['errors']:
        print(f"  warn: {err}", file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
