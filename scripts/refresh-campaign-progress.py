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

from pcs.models import Campaign  # noqa: E402
from pcs.services import (  # noqa: E402
    PROGRESS_REFRESH_LOCK_KEY,
    load_campaign_progress_snapshot,
    refresh_campaign_progress_snapshot,
)
from pcs.views import rebuild_current_task_list_html_cache  # noqa: E402
from django.core.cache import cache  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--generated-by", default="progress_refresh")
    args = parser.parse_args()

    try:
        campaign = Campaign.objects.filter(lifecycle="current").order_by("name").first()
        if campaign is None:
            print("No current campaign defined.", file=sys.stderr)
            return 2

        progress = refresh_campaign_progress_snapshot(
            campaign, generated_by=args.generated_by)
        snapshot = load_campaign_progress_snapshot(campaign) or {}
        table = rebuild_current_task_list_html_cache(
            campaign, "progress", progress_snapshot=snapshot)

        print(
            "campaign={campaign} tasks={tasks} warnings={warnings} "
            "table_bytes={table_bytes} generated_at={generated_at}".format(
                campaign=progress["campaign"],
                tasks=progress["tasks"],
                warnings=len(progress["errors"]),
                table_bytes=table["html_bytes"],
                generated_at=progress["generated_at"],
            )
        )
        return 0
    finally:
        cache.delete(PROGRESS_REFRESH_LOCK_KEY)


if __name__ == "__main__":
    raise SystemExit(main())
