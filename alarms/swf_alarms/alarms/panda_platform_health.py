"""Alarm: panda_platform_health.

The PanDA platform-health component (swf-monitor docs/SNAPPER_PLATFORM.md)
is published every five minutes with per-metric verdicts against the
SysConfig thresholds: heartbeat yield, heartbeat staleness, database
connections, server latency, monitor-host volumes and services. This
alarm raises one detection per metric in warning, plus one when the
component itself has gone silent — the platform record is the single
source; the thresholds live with the record, not here.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..common import Detection

PARAMS = {
    # A component older than this is a silent maintainer — its own alarm.
    "stale_after_minutes": 20,
    # Heartbeat verdicts on a near-empty running population are noise.
    "min_running": 200,
}

_METRIC_TEXT = {
    "heartbeat_yield": (
        "pilot heartbeats arriving below the expected rate — pilots or "
        "their outbound path are stalling"),
    "heartbeat_staleness": (
        "a large share of running jobs have not heartbeated for the "
        "configured tier — the Watcher will fail them at two hours"),
    "db_connections": "PanDA database connections near the configured limit",
    "server_latency": "PanDA server liveness request slow or failing",
    "monitor_volumes": "a monitor-host volume is above the configured use",
    "monitor_services": "the monitor's ASGI or prod-ops service is not active",
}


def _latest_platform(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT data, assessed_at FROM snapper_current_component "
            "WHERE scope = 'epicprod' AND name = 'platform' AND active")
        row = cur.fetchone()
    if not row:
        return None, None
    data = row["data"]
    if isinstance(data, str):
        data = json.loads(data)
    return data or {}, row["assessed_at"]


def detect(client, params):
    stale_after = float(params.get("stale_after_minutes", 20))
    min_running = int(params.get("min_running", 200))
    try:
        data, assessed_at = _latest_platform(client.db_conn)
    except Exception as e:                                   # noqa: BLE001
        # A failed read of the component is not a transient fetch: the
        # table is local to the engine's own database. Surface it as a
        # detection so the dashboard shows the alarm cannot see its input.
        yield Detection(
            dedupe_key="platform:unreadable",
            subject="PanDA platform alarm cannot read its component",
            body_context=(
                "Reading snapper_current_component for the platform "
                f"record failed: {e}"),
            extra_data={"error": str(e)},
        )
        return
    if data is None:
        yield Detection(
            dedupe_key="platform:absent",
            subject="PanDA platform component has never been published",
            body_context=(
                "No 'platform' component exists in the epicprod Snapper "
                "registry. The platform maintainer "
                "(monitor_app/snapper_platform.py, run by the System-status "
                "refresh) has not published."),
        )
        return
    if assessed_at is not None:
        if assessed_at.tzinfo is None:
            assessed_at = assessed_at.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - assessed_at).total_seconds() / 60
        if age_min > stale_after:
            yield Detection(
                dedupe_key="platform:silent",
                subject=(f"PanDA platform component silent for "
                         f"{age_min:.0f} min"),
                body_context=(
                    "The platform maintainer has not published within the "
                    f"{stale_after:g}-minute window. The System-status "
                    "refresh on the prod-ops agent is the publisher; check "
                    "the agent and the epicprod action stream."),
                extra_data={"age_minutes": round(age_min, 1)},
            )
            return
    assessment = data.get("assessment") or {}
    verdicts = assessment.get("verdicts") or {}
    thresholds = assessment.get("thresholds") or {}
    heartbeats = data.get("heartbeats") or {}
    running = int(heartbeats.get("running") or 0)
    database = data.get("database") or {}
    server = data.get("server") or {}
    host = data.get("monitor_host") or {}
    for metric, verdict in verdicts.items():
        if verdict != "warning" or metric not in _METRIC_TEXT:
            continue
        if metric.startswith("heartbeat") and running < min_running:
            continue
        facts = {}
        if metric == "heartbeat_yield":
            facts = {"yield": heartbeats.get("yield"),
                     "received": heartbeats.get("received"),
                     "expected": heartbeats.get("expected"),
                     "running": running}
            detail = (f"yield {heartbeats.get('yield')} — "
                      f"{heartbeats.get('received')} heartbeats received "
                      f"against {heartbeats.get('expected')} expected from "
                      f"{running} running jobs")
        elif metric == "heartbeat_staleness":
            tier = thresholds.get("platform_stale_warn_tier_minutes")
            stale = heartbeats.get(f"stale_{tier}")
            sites = sorted(
                ((s, int((e or {}).get(f"stale_{tier}") or 0))
                 for s, e in (heartbeats.get("sites") or {}).items()),
                key=lambda kv: -kv[1])[:3]
            facts = {"stale": stale, "tier_minutes": tier, "running": running,
                     "top_sites": sites}
            detail = (f"{stale} of {running} running jobs silent over "
                      f"{tier} min; " + ", ".join(
                          f"{s} {n}" for s, n in sites if n))
        elif metric == "db_connections":
            facts = {"connections": database.get("connections"),
                     "max_connections": database.get("max_connections")}
            detail = (f"{database.get('connections')} of "
                      f"{database.get('max_connections')} connections")
        elif metric == "server_latency":
            facts = {"latency_ms": server.get("latency_ms"),
                     "ok": server.get("ok"), "error": server.get("error")}
            detail = (f"is_alive {'ok' if server.get('ok') else 'NOT ok'}, "
                      f"{server.get('latency_ms')} ms"
                      + (f" — {server.get('error')}" if server.get("error") else ""))
        elif metric == "monitor_volumes":
            vols = {p: (v or {}).get("used_percent")
                    for p, v in (host.get("volumes") or {}).items()}
            facts = {"volumes": vols}
            detail = ", ".join(f"{p} {u}%" for p, u in vols.items()
                               if u is not None)
        else:
            facts = {"asgi": host.get("asgi"), "ops_agent": host.get("ops_agent")}
            detail = (f"ASGI {((host.get('asgi') or {}).get('state'))}, "
                      f"prod-ops agent {((host.get('ops_agent') or {}).get('state'))}")
        yield Detection(
            dedupe_key=f"platform:{metric}",
            subject=f"PanDA platform: {metric.replace('_', ' ')} — {detail}",
            body_context=(
                f"{_METRIC_TEXT[metric]}. {detail}. Thresholds are the "
                "platform_* SysConfig keys; the recorded history and the "
                "summary at any instant are on the Platform view "
                "(/snapper/epicprod/platform/)."),
            extra_data={"metric": metric, **facts,
                        "assessed_at": assessed_at.isoformat() if assessed_at else None},
        )
