"""Alarm: catalog_sync_freshness.

The nightly catalog_sync chain (cron 02:15 -> ops agent) keeps the production
catalog complete: requests, associations and auto-intake, Rucio snapshots.
This alarm fires when the newest successful catalog_sync summary record in
the epicprod action stream is older than the configured maximum age — the
staleness that automation exists to prevent, surfaced instead of silent.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..common import Detection
from ..common.actions import latest_action

PARAMS = {
    # One missed nightly plus slack. Two misses is unambiguous.
    "max_age_hours": 30,
}


def detect(client, params):
    max_age = float(params.get("max_age_hours", 30))
    row = latest_action(client.db_conn, "catalog_sync", outcome="ok")
    if row is None:
        yield Detection(
            dedupe_key="catalog_sync:never",
            subject="catalog sync has never recorded a successful run",
            body_context=(
                "No successful catalog_sync summary record exists in the "
                "epicprod action stream. The nightly catalog synchronization "
                "(cron 02:15 -> ops agent) is not running or not completing."),
            extra_data={"max_age_hours": max_age},
        )
        return
    ts = row["timestamp"]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    if age_hours > max_age:
        yield Detection(
            dedupe_key="catalog_sync:stale",
            subject=(f"catalog sync stale: last success "
                     f"{age_hours:.1f} h ago (max {max_age:g} h)"),
            body_context=(
                "The nightly catalog_sync chain has not completed "
                f"successfully in {age_hours:.1f} hours. Check the ops agent "
                "and the epicprod action stream (Logs page, "
                "app_name=epicprod) for step failures; run manually with "
                "scripts/enqueue-ops-message.py catalog_sync."),
            extra_data={"age_hours": round(age_hours, 1),
                        "max_age_hours": max_age,
                        "last_success": ts.isoformat()},
        )
