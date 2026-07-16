#!/usr/bin/env python3
"""Refresh current campaign progress data and rendered table cache."""
import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "swf_monitor_project.settings")

import django  # noqa: E402

django.setup()

# Provenance probe: a warning in this doer's output cited a dev-venv Django
# path that no direct probe of the deployed venv reproduces; report where
# this subprocess actually runs from (2026-07-12).
print(f"interpreter={sys.executable} django={django.__file__}", file=sys.stderr)

from pcs.models import Campaign  # noqa: E402
from pcs.services import (  # noqa: E402
    load_campaign_progress_snapshot,
    refresh_campaign_progress_snapshot,
)
from pcs.views import rebuild_current_task_list_html_cache  # noqa: E402
from swf_epicprod.analytics.rollup import campaign_status  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--generated-by", default="progress_refresh")
    args = parser.parse_args()

    from pcs.views import _campaigns_with_inflow

    # Producing campaigns are the assessment targets and lead the set;
    # usually identical to current, distinct around lifecycle transitions.
    targets = [camp for camp, _ in _campaigns_with_inflow()]
    current = Campaign.objects.filter(lifecycle="current").order_by("name").first()
    if current is not None and all(c.pk != current.pk for c in targets):
        targets.append(current)
    if not targets:
        print("No producing or current campaign defined.", file=sys.stderr)
        return 2

    for campaign in targets:
        progress = refresh_campaign_progress_snapshot(
            campaign, generated_by=args.generated_by)
        snapshot = load_campaign_progress_snapshot(campaign) or {}
        table = rebuild_current_task_list_html_cache(
            campaign, "progress", progress_snapshot=snapshot)
        # The catalog view has no other clockwork rebuilder — without this it
        # serves its stale copy indefinitely (page-load rebuild is suppressed).
        catalog_table = rebuild_current_task_list_html_cache(campaign, "catalog")
        analytics = campaign_status(
            campaign.name, window_days=1, record=True,
            generated_by=args.generated_by)
        print(
            "campaign={campaign} tasks={tasks} warnings={warnings} "
            "table_bytes={table_bytes} catalog_table_bytes={catalog_bytes} "
            "generated_at={generated_at} analytics_at={analytics_at}".format(
                campaign=progress["campaign"],
                tasks=progress["tasks"],
                warnings=len(progress["errors"]),
                table_bytes=table["html_bytes"],
                catalog_bytes=catalog_table["html_bytes"],
                generated_at=progress["generated_at"],
                analytics_at=analytics["generated_at"],
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
