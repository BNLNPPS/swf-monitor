"""Capcom transition notices for SWF alarms.

One notice when an alarm event fires and one when it clears —
transitions only, never periodic checks or watchdog output. Posted to
the tjai Capcom ingest endpoint (listen source ``swf-alarms``) with the
bearer credential from ``TJAI_API_KEY``; the engine cron sources the
environment file that carries it. A failed post is logged and dropped —
the alarm record itself is the durable truth.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)

INGEST_URL = "https://etaverse.com/tjai/api/capcom/notice"
ALARMS_PAGE = "https://epic-devcloud.org/prod/alarms/"
TIMEOUT = 20


def post_transition(alarm_entry_id: str, dedupe_key: str,
                    transition: str, subject: str) -> None:
    """POST one fire/clear transition notice; never raises."""
    token = os.environ.get("TJAI_API_KEY", "")
    if not token:
        log.error("capcom: TJAI_API_KEY unset; %s notice for %s dropped",
                  transition, alarm_entry_id)
        return
    fired = transition == "fired"
    payload = {
        "source": "swf-alarms",
        "severity": "alarm" if fired else "info",
        "title": (f"SWF alarm: {subject}" if fired
                  else f"SWF alarm cleared: {subject}"),
        "url": ALARMS_PAGE,
        "dedup_key": f"swf-alarm:{alarm_entry_id}:{dedupe_key}:{transition}",
    }
    req = urllib.request.Request(
        INGEST_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                log.error("capcom: HTTP %s for %s", resp.status,
                          payload["dedup_key"])
            else:
                log.info("capcom: posted %s", payload["dedup_key"])
    except Exception as e:  # noqa: BLE001
        log.error("capcom: post failed for %s: %s", payload["dedup_key"], e)
