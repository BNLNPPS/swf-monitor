"""Alarm: payload_fetch_rate.

The payload-log endpoint is the one open-face URL whose GET triggers
credentialed agent work (Rucio/xrootd fetch plus cache disk). A polite
consumer costs nothing; a crawler or runaway script does not — Googlebot
demonstrated this within hours of the action stream going live (2026-07-05,
one fetch every 72 s until blocked). This alarm watches the fetch rate in
the action stream so the next enthusiastic client is caught by machinery.
"""
from __future__ import annotations

from ..common import Detection
from ..common.actions import action_count

PARAMS = {
    "window_minutes": 60,
    # Human investigation runs tens per hour; the Googlebot incident ran ~50.
    "max_fetches": 60,
}


def detect(client, params):
    window = int(params.get("window_minutes", 60))
    cap = int(params.get("max_fetches", 60))
    n = action_count(client.db_conn, "payload_log_fetch", minutes=window)
    if n > cap:
        yield Detection(
            dedupe_key="payload_fetch_rate",
            subject=(f"payload-log fetch rate high: {n} in "
                     f"{window} min (cap {cap})"),
            body_context=(
                "Payload-log fetches trigger credentialed agent work. A rate "
                "above the cap suggests a crawler or runaway script on the "
                "open face. Check the action stream (Logs page, "
                "app_name=epicprod, action=payload_log_fetch) and the "
                "devcloud proxy logs for the client; robots.txt and the "
                "proxy 403 govern crawler access."),
            extra_data={"count": n, "window_minutes": window,
                        "max_fetches": cap},
        )
