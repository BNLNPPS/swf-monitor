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
    "pandamon_latency": (
        "PanDA monitor (BigPanDA) web face slow or failing — its "
        "queries are also load on the PanDA database"),
    "monitor_volumes": "a monitor-host volume is above the configured use",
    "monitor_services": "the monitor's ASGI or prod-ops service is not active",
    "submit_workers_held": (
        "pilots are held on the submit host — a site refusing every "
        "submission holds them here and appears in no PanDA record; "
        "GREX held fifteen over thirteen hours unnoticed"),
    "submit_daemons": (
        "a harvester daemon that ticks regardless of demand has stopped "
        "writing its log — submission has stalled"),
    "submit_processes": (
        "the harvester or the submit schedd is not running on the "
        "submit host — nothing is being submitted at all"),
    "server_units": (
        "a PanDA unit (httpd, daemon, JEDI, MCP) is not active on the "
        "server host"),
    "server_daemons": (
        "a PanDA daemon that ticks regardless of demand has stopped "
        "writing its log on the server host"),
    "server_5xx": (
        "the PanDA web tier is answering a large share of requests with "
        "5xx — pilots and harvester are failing their calls"),
    "server_web_errors": (
        "the PanDA web tier's error log carries saturation, timeout, or "
        "crash markers in the interval"),
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
    pandamon = data.get("pandamon") or {}
    host = data.get("monitor_host") or {}
    for metric, verdict in verdicts.items():
        if verdict != "warning" or metric not in _METRIC_TEXT:
            continue
        if metric.startswith("heartbeat") and running < min_running:
            continue
        facts = {}
        if metric == "heartbeat_yield":
            # The verdict is on the windowed yield (two heartbeat
            # periods, ratio of sums); older records carry only the
            # per-interval figure.
            window = heartbeats.get("window") or {}
            basis = window if window.get("yield") is not None else heartbeats
            minutes = round(int(window.get("seconds") or 0) / 60)
            span = (f"over {minutes} min ({window.get('intervals')} intervals)"
                    if window.get("yield") is not None else "in the interval")
            facts = {"yield": basis.get("yield"),
                     "received": basis.get("received"),
                     "expected": basis.get("expected"),
                     "window_minutes": minutes or None,
                     "running": running}
            detail = (f"yield {basis.get('yield')} {span} — "
                      f"{basis.get('received')} heartbeats received "
                      f"against {basis.get('expected')} expected from "
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
        elif metric == "pandamon_latency":
            facts = {probe: {"latency_ms": (p or {}).get("latency_ms"),
                             "ok": (p or {}).get("ok"),
                             "error": (p or {}).get("error")}
                     for probe, p in pandamon.items()}
            detail = ", ".join(
                f"{probe} {'ok' if (p or {}).get('ok') else 'NOT ok'} "
                f"{(p or {}).get('latency_ms')} ms"
                + (f" ({p.get('error')})" if (p or {}).get("error") else "")
                for probe, p in pandamon.items())
        elif metric == "monitor_volumes":
            vols = {p: (v or {}).get("used_percent")
                    for p, v in (host.get("volumes") or {}).items()}
            facts = {"volumes": vols}
            detail = ", ".join(f"{p} {u}%" for p, u in vols.items()
                               if u is not None)
        elif metric.startswith("submit_"):
            submit = data.get("submit_host") or {}
            if metric == "submit_workers_held":
                workers = submit.get("workers") or {}
                reasons = submit.get("held_reasons") or {}
                facts = {"workers": workers, "held_reasons": reasons}
                # The reason is what a site needs; the count alone says
                # nothing about whether it is a site refusing work.
                top = sorted(reasons.items(), key=lambda kv: kv[1],
                             reverse=True)[:3]
                detail = (f"{workers.get('held')} held"
                          + (": " + "; ".join(f"{n}x {r[:160]}"
                                              for r, n in top) if top else ""))
            elif metric == "submit_daemons":
                facts = {"oldest": submit.get("daemon_oldest_name"),
                         "oldest_log_seconds":
                             submit.get("daemon_oldest_log_seconds"),
                         "daemons": submit.get("daemons")}
                detail = (f"{submit.get('daemon_oldest_name')} last wrote "
                          f"{submit.get('daemon_oldest_log_seconds')} s ago")
            else:
                facts = {"harvester_process": submit.get("harvester_process"),
                         "schedd_process": submit.get("schedd_process")}
                detail = (
                    f"harvester {'up' if submit.get('harvester_process') else 'DOWN'}, "
                    f"schedd {'up' if submit.get('schedd_process') else 'DOWN'}")
        elif metric in ("server_units", "server_daemons", "server_5xx",
                        "server_web_errors"):
            srv = data.get("server_host") or {}
            if metric == "server_units":
                units = srv.get("units") or {}
                facts = {"units": units}
                detail = ", ".join(
                    f"{unit} {(entry or {}).get('active') or 'unknown'}"
                    for unit, entry in sorted(units.items()))
            elif metric == "server_daemons":
                facts = {"oldest": srv.get("daemon_oldest_name"),
                         "oldest_log_seconds":
                             srv.get("daemon_oldest_log_seconds"),
                         "daemons": srv.get("daemons")}
                detail = (f"{srv.get('daemon_oldest_name')} last wrote "
                          f"{srv.get('daemon_oldest_log_seconds')} s ago")
            elif metric == "server_5xx":
                facts = {"requests": srv.get("requests"),
                         "status_5xx": srv.get("status_5xx"),
                         "by_status_class": srv.get("by_status_class"),
                         "interval_seconds": srv.get("interval_seconds")}
                detail = (f"{srv.get('status_5xx')} of {srv.get('requests')} "
                          f"requests answered 5xx over "
                          f"{srv.get('interval_seconds')} s")
            else:
                markers = {k: v for k, v in
                           (srv.get("error_markers") or {}).items() if v}
                facts = {"markers": markers,
                         "recent": srv.get("error_recent")}
                detail = ", ".join(f"{k} {v}" for k, v in sorted(markers.items()))
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
