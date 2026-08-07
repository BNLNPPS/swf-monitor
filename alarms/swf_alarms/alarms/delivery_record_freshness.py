"""Alarm: delivery_record_freshness.

The Snapper campaign view's curves draw from the daily delivered-data
record: one snap per complete ET day, rebuilt nightly by the
catalog_sync chain's delivery_daily_rebuild step (capture policy
delivery-daily-v1). This alarm fires when the newest daily delivery
snap is older than the configured maximum age — the record silently
stalling is exactly the failure that hid a week of production from the
campaign plot (July 27 – Aug 4, 2026) before the nightly step existed.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..common import Detection

PARAMS = {
    # A snap for day D is stamped D 23:59:59 ET and written by the
    # ~02:47 ET chain, so a healthy record's newest snap is at most
    # ~27 h old just before the next rebuild. 30 h means one missed
    # night, detected the same morning.
    "max_age_hours": 30,
}

QUERY = """
    SELECT max(snap_time) AS newest FROM snapper_system_snap
    WHERE scope = 'epicprod'
      AND capture_policy IN ('backfill-v1', 'delivery-daily-v1')
"""


def detect(client, params):
    max_age = float(params.get("max_age_hours", 30))
    with client.db_conn.cursor() as cur:
        cur.execute(QUERY)
        row = cur.fetchone()
    newest = row["newest"] if row else None
    if newest is None:
        yield Detection(
            dedupe_key="delivery_record:never",
            subject="campaign delivery daily record has never been built",
            body_context=(
                "No daily delivery snap exists in the epicprod Snapper "
                "scope. The Snapper campaign view has no curve data. Run "
                "the delivery_daily_rebuild step (enqueue-ops-message.py "
                "delivery_daily_rebuild) or the full catalog_sync chain."),
            extra_data={"max_age_hours": max_age},
        )
        return
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - newest).total_seconds() / 3600.0
    if age_hours > max_age:
        yield Detection(
            dedupe_key="delivery_record:stale",
            subject=(f"campaign delivery record stale: newest daily snap "
                     f"{age_hours:.1f} h old (max {max_age:g} h)"),
            body_context=(
                "The daily delivered-data record behind the Snapper "
                "campaign view has not advanced. The nightly "
                "delivery_daily_rebuild step of the catalog_sync chain "
                "(prod-ops agent, ~02:47 ET) is not running or not "
                "completing. Check the epicprod action stream (Logs page, "
                "app_name=epicprod, action=delivery_daily_rebuild) for "
                "step failures; run manually with "
                "scripts/enqueue-ops-message.py delivery_daily_rebuild."),
            extra_data={"age_hours": round(age_hours, 1),
                        "max_age_hours": max_age,
                        "newest_snap": newest.isoformat()},
        )
