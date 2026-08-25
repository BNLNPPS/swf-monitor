"""Publish the epicprod PanDA platform-health component to Snapper.

Design: docs/SNAPPER_PLATFORM.md. Each publication carries the owner's
assessed reading of the platform at one instant, in five groups:

- database: PanDA database connections, longest transaction, and the
  jobsactive4 table's health, read from the PanDA database;
- heartbeats: running jobs by heartbeat age, heartbeats received and
  jobs started in the publication interval, and the heartbeat yield
  against the running population's expected rate;
- server: one timed liveness request to the PanDA server;
- server_host: the pandaserver01 reporter's record when one has been
  delivered (docs/PANDA_SERVER_REPORTER.md), else absent;
- monitor_host: the monitor's own host and tier, measured locally.

Load (jobs in flight, cores) and consequences (kills, outcomes) are
not recorded here: the PanDA activity and error-state components
carry them at the same cadence, and the Platform view reads them from
there. Every measurement that fails records its failure in place and
the publication proceeds; a source that cannot be read is an assessed
unavailable state, never a silent omission.
"""

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.db import connection, connections, transaction
from django.utils import timezone

from snapper_ai.services import (
    ComponentUpdate,
    publish_component,
    register_component,
)

from .panda.constants import PANDA_SCHEMA
from .snapper_panda import _bounded_site_names, _canary_queue_names

PUBLISHER_IDENTITY = "swf-monitor:panda-platform"
ASSESSMENT_POLICY_VERSION = "swf-panda-platform-v1"
COMPONENT_NAME = "platform"
SCOPE = "epicprod"

MAX_SITES = 32
MAX_APPS = 16
MAX_SERIALIZED_BYTES = 64 * 1024
# Heartbeat-age tiers in minutes: part of the recorded vocabulary (the
# view's curves are named by them), so a constant, not a knob.
STALENESS_TIERS_MINUTES = (30, 60, 120)
# A fresh component starts its first interval this far back rather
# than swallowing all recorded history into one interval.
FIRST_INTERVAL_MINUTES = 5
# Units and volumes measured on the monitor host.
MONITOR_UNITS = {
    "asgi": "swf-monitor-mcp-asgi",
    "ops_agent": "epicprod-ops-agent",
    "httpd": "httpd",
}

# Operator-visible configuration, seeded at first read (SysConfig
# sets things; no knob hides behind a code default).
CONFIG_DEFAULTS = {
    "platform_panda_server_url": "https://pandaserver01.sdcc.bnl.gov:25443",
    "platform_server_timeout_seconds": 10,
    "platform_pandamon_url": "https://pandamon01.sdcc.bnl.gov",
    "platform_pandamon_timeout_seconds": 20,
    "platform_heartbeat_period_seconds": 1800,
    "platform_monitor_volumes": ["/", "/var", "/data"],
    "platform_reporter_stale_seconds": 900,
    # Assessment thresholds.
    "platform_yield_warn_below": 0.5,
    "platform_connections_warn_fraction": 0.8,
    "platform_latency_warn_ms": 2000,
    "platform_pandamon_warn_ms": 20000,
    "platform_stale_warn_fraction": 0.25,
    "platform_stale_warn_tier_minutes": 60,
    "platform_volume_warn_percent": 90,
}

PLATFORM_REGISTRATION = {
    "title": "PanDA platform health",
    "description": (
        "Five-minute assessed readings of the PanDA platform: database "
        "connections and table health, running-job heartbeat ages and "
        "the heartbeat yield over the publication interval, PanDA "
        "server liveness latency, the server host's own report when "
        "delivered, and the monitor host's tier. Load and consequences "
        "are recorded by the PanDA activity and error-state components."
    ),
    "visibility": "public",
    "owning_subsystem": "SWF PanDA production monitor",
    "assessment_policy": ASSESSMENT_POLICY_VERSION,
    "max_serialized_bytes": MAX_SERIALIZED_BYTES,
    "quantities": {
        "interval": {
            "path": "interval",
            "type": "object",
            "required": True,
            "kind": "window",
            "description": (
                "The half-open interval (start, end] the interval "
                "quantities (heartbeats received, starts) cover; runs "
                "from the previous publication's source time."
            ),
        },
        "database": {
            "path": "database",
            "type": "object",
            "required": True,
            "kind": "gauge",
            "description": (
                "PanDA database: connections total/active/idle/waiting "
                "against max_connections, the longest open transaction "
                "in seconds, jobsactive4 live and dead tuples and minutes "
                "since its last autovacuum, and connections by "
                "application and state (bounded map, remainder in "
                "'other'). An 'error' key records a failed read."
            ),
        },
        "database_by_app": {
            "path": "database.by_app",
            "type": "object",
            "required": False,
            "kind": "bounded_map",
            "max_items": MAX_APPS,
            "description": "Connections by application name and state.",
        },
        "heartbeats": {
            "path": "heartbeats",
            "type": "object",
            "required": True,
            "kind": "assessment",
            "description": (
                "Running jobs by heartbeat age (stale_30, stale_60, "
                "stale_120: last modification older than N minutes), "
                "heartbeats received and jobs started in the interval, "
                "the expected heartbeat count for the running population "
                "at the configured heartbeat period, and the yield "
                "(received over expected)."
            ),
        },
        "heartbeat_sites": {
            "path": "heartbeats.sites",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": MAX_SITES,
            "description": (
                "Per-site running counts and heartbeat-age tiers. Every "
                "non-test Canary queue is retained; remaining slots take "
                "the sites with the most running jobs."
            ),
        },
        "server": {
            "path": "server",
            "type": "object",
            "required": True,
            "kind": "gauge",
            "description": (
                "One timed liveness request to the PanDA server: "
                "latency in milliseconds, HTTP status, and ok; a timeout "
                "or failure is recorded with its reason."
            ),
        },
        "pandamon": {
            "path": "pandamon",
            "type": "object",
            "required": False,
            "kind": "gauge",
            "description": (
                "Two timed requests to the PanDA monitor (BigPanDA) web "
                "face: its front page and the harvester worker-stats "
                "query the monitor's tools use; latency in milliseconds, "
                "HTTP status, and ok; a timeout or failure is recorded "
                "with its reason."
            ),
        },
        "server_host": {
            "path": "server_host",
            "type": "object",
            "required": False,
            "kind": "gauge",
            "description": (
                "The pandaserver01 reporter's latest record (web-tier "
                "request counts, daemon liveness, WSGI tier, host "
                "resources), present only when one has been delivered."
            ),
        },
        "reporter_status": {
            "path": "reporter_status",
            "type": "string",
            "required": True,
            "kind": "assessment",
            "enum": ["fresh", "stale", "absent"],
            "description": (
                "Freshness of the server-host report against the "
                "configured staleness threshold."
            ),
        },
        "monitor_host": {
            "path": "monitor_host",
            "type": "object",
            "required": True,
            "kind": "gauge",
            "description": (
                "The monitor host: load average, memory and swap, volume "
                "use, the WSGI daemon and httpd processes, the ASGI and "
                "prod-ops agent services, and the monitor database's "
                "connection count."
            ),
        },
        "assessment": {
            "path": "assessment",
            "type": "object",
            "required": True,
            "kind": "assessment",
            "description": (
                "Per-metric verdicts (ok, warning, unknown) against the "
                "SysConfig thresholds, the thresholds applied, and the "
                "overall verdict."
            ),
        },
    },
}


@dataclass(frozen=True)
class PlatformPublication:
    registration_update: ComponentUpdate
    update: ComponentUpdate
    projection: dict
    observed_at: datetime


def _config(key):
    from .models import SysConfig

    return SysConfig.get_setting(key, CONFIG_DEFAULTS[key])


def _iso_utc(value):
    if value.tzinfo is not None:
        value = value.astimezone(dt_timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="seconds") + "Z"


def _naive_utc(value):
    """The PanDA database stores naive UTC timestamps."""
    return value.astimezone(dt_timezone.utc).replace(tzinfo=None)


# ── Database ────────────────────────────────────────────────────────────

def database_reading():
    """Connections, longest transaction, and jobsactive4 health from
    the PanDA database's own statistics views."""
    out = {}
    try:
        with connections["panda"].cursor() as cursor:
            cursor.execute(
                "SELECT setting::int FROM pg_settings "
                "WHERE name = 'max_connections'")
            out["max_connections"] = int(cursor.fetchone()[0])
            cursor.execute(
                "SELECT COALESCE(state, ''), "
                "       (wait_event IS NOT NULL AND state = 'active'), "
                "       COUNT(*) "
                "FROM pg_stat_activity WHERE datname = current_database() "
                "GROUP BY 1, 2")
            total = active = idle = waiting = 0
            for state, is_waiting, count in cursor.fetchall():
                count = int(count or 0)
                total += count
                if state == "active":
                    active += count
                    if is_waiting:
                        waiting += count
                elif state.startswith("idle"):
                    idle += count
            out.update({"connections": total, "active": active,
                        "idle": idle, "waiting": waiting})
            cursor.execute(
                "SELECT COALESCE(EXTRACT(EPOCH FROM MAX(now() - xact_start)), 0) "
                "FROM pg_stat_activity "
                "WHERE datname = current_database() AND state <> 'idle'")
            out["longest_transaction_s"] = round(float(cursor.fetchone()[0] or 0), 1)
            cursor.execute(
                "SELECT n_live_tup, n_dead_tup, last_autovacuum "
                "FROM pg_stat_user_tables "
                "WHERE schemaname = %s AND relname = 'jobsactive4'",
                [PANDA_SCHEMA])
            row = cursor.fetchone()
            if row:
                live, dead, last_vacuum = row
                out["jobsactive4"] = {
                    "live_tuples": int(live or 0),
                    "dead_tuples": int(dead or 0),
                    "minutes_since_autovacuum": (
                        round((timezone.now() - last_vacuum).total_seconds() / 60)
                        if last_vacuum is not None else None),
                }
            cursor.execute(
                "SELECT COALESCE(NULLIF(application_name, ''), 'unnamed'), "
                "       COALESCE(state, ''), COUNT(*) "
                "FROM pg_stat_activity WHERE datname = current_database() "
                "GROUP BY 1, 2 ORDER BY 3 DESC")
            by_app = {}
            for app, state, count in cursor.fetchall():
                key = f"{app}@{state}" if state else str(app)
                by_app[key] = int(count or 0)
            if len(by_app) > MAX_APPS:
                keep = dict(list(by_app.items())[:MAX_APPS - 1])
                keep["other"] = sum(list(by_app.values())[MAX_APPS - 1:])
                by_app = keep
            out["by_app"] = by_app
    except Exception as e:                                   # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"[:300]
    return out


# ── Heartbeats ──────────────────────────────────────────────────────────

def heartbeat_reading(mark, until, period_seconds):
    """Running jobs by heartbeat age (scope and per site), heartbeats
    received and jobs started in (mark, until], and the yield against
    the running population's expected heartbeat count."""
    out = {"tiers_minutes": list(STALENESS_TIERS_MINUTES)}
    until_naive = _naive_utc(until)
    mark_naive = _naive_utc(mark)
    tier_filters = ", ".join(
        f"COUNT(*) FILTER (WHERE \"modificationtime\" < %s)"
        for _ in STALENESS_TIERS_MINUTES)
    tier_params = [until_naive - timedelta(minutes=m)
                   for m in STALENESS_TIERS_MINUTES]
    try:
        with connections["panda"].cursor() as cursor:
            cursor.execute(
                f"SELECT COALESCE(\"computingsite\", 'unknown'), COUNT(*), "
                f"       {tier_filters}, "
                f"       COUNT(*) FILTER (WHERE \"modificationtime\" > %s) "
                f"FROM \"{PANDA_SCHEMA}\".\"jobsactive4\" "
                f"WHERE \"jobstatus\" = 'running' "
                f"GROUP BY 1",
                tier_params + [mark_naive])
            per_site = {}
            running = received = 0
            stale = [0] * len(STALENESS_TIERS_MINUTES)
            for row in cursor.fetchall():
                site = str(row[0])
                count = int(row[1] or 0)
                tiers = [int(v or 0) for v in row[2:2 + len(STALENESS_TIERS_MINUTES)]]
                got = int(row[2 + len(STALENESS_TIERS_MINUTES)] or 0)
                running += count
                received += got
                stale = [a + b for a, b in zip(stale, tiers)]
                entry = {"running": count, "received": got}
                for minutes, value in zip(STALENESS_TIERS_MINUTES, tiers):
                    entry[f"stale_{minutes}"] = value
                per_site[site] = entry
            cursor.execute(
                f"SELECT COUNT(*) FROM ("
                f"  SELECT \"pandaid\" FROM \"{PANDA_SCHEMA}\".\"jobsactive4\" "
                f"  WHERE \"starttime\" > %s AND \"starttime\" <= %s "
                f"  UNION "
                f"  SELECT \"pandaid\" FROM \"{PANDA_SCHEMA}\".\"jobsarchived4\" "
                f"  WHERE \"starttime\" > %s AND \"starttime\" <= %s) s",
                [mark_naive, until_naive, mark_naive, until_naive])
            started = int(cursor.fetchone()[0] or 0)
    except Exception as e:                                   # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"[:300]
        out.update({"running": 0, "received": 0, "started": 0,
                    "expected": 0, "yield": None, "sites": {}})
        for minutes in STALENESS_TIERS_MINUTES:
            out[f"stale_{minutes}"] = 0
        return out

    interval_seconds = max(0.0, (until - mark).total_seconds())
    expected = round(running * interval_seconds / max(period_seconds, 1))
    out.update({
        "running": running,
        "received": received,
        "started": started,
        "expected": expected,
        "yield": (round(min(received / expected, 9.999), 3)
                  if expected else None),
        "period_seconds": int(period_seconds),
    })
    for minutes, value in zip(STALENESS_TIERS_MINUTES, stale):
        out[f"stale_{minutes}"] = value
    catalog = _canary_queue_names()
    names = _bounded_site_names(
        per_site, catalog,
        lambda name: (-int((per_site.get(name) or {}).get("running") or 0),
                      name))
    sites = {}
    for name in names:
        entry = per_site.get(name)
        if entry is None:
            entry = {"running": 0, "received": 0}
            for minutes in STALENESS_TIERS_MINUTES:
                entry[f"stale_{minutes}"] = 0
        sites[name] = entry
    out["sites"] = sites
    return out


# ── Server ──────────────────────────────────────────────────────────────

def timed_get(url, timeout_seconds, params=None):
    """One timed GET: latency in milliseconds, HTTP status, ok; a
    timeout records at the timeout value with its reason, never omitted."""
    import requests

    started = time.monotonic()
    try:
        response = requests.get(url, params=params,
                                timeout=float(timeout_seconds))
        ms = round((time.monotonic() - started) * 1000, 1)
        return {"url": url, "latency_ms": ms,
                "status": int(response.status_code),
                "ok": response.status_code == 200,
                "timeout": False}
    except requests.Timeout:
        return {"url": url, "latency_ms": round(float(timeout_seconds) * 1000, 1),
                "status": None, "ok": False, "timeout": True,
                "error": f"no response within {timeout_seconds}s"}
    except Exception as e:                                   # noqa: BLE001
        return {"url": url,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "status": None, "ok": False, "timeout": False,
                "error": f"{type(e).__name__}: {e}"[:300]}


def server_reading(base_url, timeout_seconds):
    """One timed request to the PanDA server's liveness endpoint."""
    return timed_get(f"{base_url.rstrip('/')}/api/v1/system/is_alive",
                     timeout_seconds)


def pandamon_reading(base_url, timeout_seconds, now):
    """Two timed requests to the PanDA monitor (BigPanDA) web face: the
    front page, and the harvester worker-stats query over the last hour
    — the request the monitor's own tools make, so the record shows the
    face as its consumers meet it."""
    from concurrent.futures import ThreadPoolExecutor

    root = base_url.rstrip("/")
    since = now - timedelta(hours=1)
    # The probes run together: the reading costs one timeout at worst,
    # not one per probe, inside the refresh doer's own time limit.
    with ThreadPoolExecutor(max_workers=2) as pool:
        front = pool.submit(timed_get, f"{root}/", timeout_seconds)
        workers = pool.submit(
            timed_get, f"{root}/harvester/getworkerstats/", timeout_seconds,
            {"lastupdate_from": since.strftime("%Y-%m-%d %H:%M:%S"),
             "lastupdate_to": now.strftime("%Y-%m-%d %H:%M:%S")})
        return {"front": front.result(), "workers": workers.result()}


# ── Server host (reporter) ──────────────────────────────────────────────

def server_host_reading(now, stale_seconds):
    """The pandaserver01 reporter's latest record and its freshness.
    Until the reporter and its ingest exist (docs/SNAPPER_PLATFORM.md,
    delivery stage 3) there is no record: status 'absent'."""
    return None, "absent"


# ── Monitor host ────────────────────────────────────────────────────────

def _rss_kb(pid):
    try:
        with open(f"/proc/{pid}/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def _unit_state(unit):
    try:
        result = subprocess.run(
            ["systemctl", "show", unit,
             "-p", "ActiveState,MainPID,NRestarts"],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"{type(e).__name__}: {e}"[:200]}
    props = dict(line.split("=", 1) for line in result.stdout.splitlines()
                 if "=" in line)
    pid = int(props.get("MainPID") or 0)
    rss = _rss_kb(pid) if pid else None
    return {"active": props.get("ActiveState") == "active",
            "state": props.get("ActiveState") or "unknown",
            "restarts": int(props.get("NRestarts") or 0),
            "rss_mb": round(rss / 1024, 1) if rss is not None else None}


def _process_scan():
    """httpd process count and resident memory, and the mod_wsgi
    daemon processes (display name 'wsgi:...') separately."""
    httpd = {"count": 0, "rss_mb": 0.0}
    wsgi = {"count": 0, "rss_mb": 0.0}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm") as handle:
                comm = handle.read().strip()
            if comm != "httpd":
                continue
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                cmdline = handle.read().replace(b"\0", b" ").decode(
                    "utf-8", "replace")
        except OSError:
            continue
        rss = _rss_kb(entry) or 0
        httpd["count"] += 1
        httpd["rss_mb"] += rss / 1024
        if "wsgi:" in cmdline:
            wsgi["count"] += 1
            wsgi["rss_mb"] += rss / 1024
    httpd["rss_mb"] = round(httpd["rss_mb"], 1)
    wsgi["rss_mb"] = round(wsgi["rss_mb"], 1)
    return httpd, wsgi


def monitor_host_reading(volumes):
    out = {}
    try:
        with open("/proc/loadavg") as handle:
            parts = handle.read().split()
        out["load"] = {"1m": float(parts[0]), "5m": float(parts[1]),
                       "15m": float(parts[2])}
        out["cpus"] = os.cpu_count()
    except (OSError, ValueError, IndexError) as e:
        out["load_error"] = f"{type(e).__name__}: {e}"[:200]
    try:
        info = {}
        with open("/proc/meminfo") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                info[key] = int(rest.split()[0])
        total = info["MemTotal"]
        available = info["MemAvailable"]
        out["memory"] = {
            "total_mb": round(total / 1024),
            "available_mb": round(available / 1024),
            "used_mb": round((total - available) / 1024),
            "used_percent": round(100 * (total - available) / total, 1),
            "swap_used_mb": round((info["SwapTotal"] - info["SwapFree"]) / 1024),
        }
    except (OSError, ValueError, KeyError) as e:
        out["memory_error"] = f"{type(e).__name__}: {e}"[:200]
    vols = {}
    for path in volumes:
        try:
            stat = os.statvfs(path)
            total = stat.f_blocks * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            vols[path] = {"used_percent": round(100 * (total - free) / total, 1)
                          if total else None,
                          "free_gb": round(free / 1e9, 1)}
        except OSError as e:
            vols[path] = {"error": f"{type(e).__name__}: {e}"[:200]}
    out["volumes"] = vols
    try:
        httpd, wsgi = _process_scan()
        out["httpd"] = httpd
        out["wsgi"] = wsgi
    except OSError as e:
        out["process_error"] = f"{type(e).__name__}: {e}"[:200]
    out["asgi"] = _unit_state(MONITOR_UNITS["asgi"])
    out["ops_agent"] = _unit_state(MONITOR_UNITS["ops_agent"])
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM pg_stat_activity "
                "WHERE datname = current_database()")
            out["db_connections"] = int(cursor.fetchone()[0] or 0)
    except Exception as e:                                   # noqa: BLE001
        out["db_error"] = f"{type(e).__name__}: {e}"[:200]
    return out


# ── Assessment ──────────────────────────────────────────────────────────

def assess(database, heartbeats, server, reporter_status, monitor_host,
           thresholds, pandamon=None):
    """Per-metric verdicts against the configured thresholds."""
    verdicts = {}

    def verdict(name, condition_warning, known=True):
        verdicts[name] = ("unknown" if not known
                          else "warning" if condition_warning else "ok")

    y = heartbeats.get("yield")
    verdict("heartbeat_yield",
            y is not None and y < thresholds["platform_yield_warn_below"],
            known=y is not None and "error" not in heartbeats)
    running = int(heartbeats.get("running") or 0)
    tier = int(thresholds["platform_stale_warn_tier_minutes"])
    stale = int(heartbeats.get(f"stale_{tier}") or 0)
    verdict("heartbeat_staleness",
            running > 0 and stale / running
            > thresholds["platform_stale_warn_fraction"],
            known="error" not in heartbeats)
    conns = database.get("connections")
    limit = database.get("max_connections")
    verdict("db_connections",
            bool(conns and limit) and conns / limit
            > thresholds["platform_connections_warn_fraction"],
            known="error" not in database and bool(limit))
    verdict("server_latency",
            (not server.get("ok"))
            or server.get("latency_ms", 0) > thresholds["platform_latency_warn_ms"],
            known=True)
    probes = list((pandamon or {}).values())
    verdict("pandamon_latency",
            any((not p.get("ok"))
                or p.get("latency_ms", 0) > thresholds["platform_pandamon_warn_ms"]
                for p in probes),
            known=bool(probes))
    vols = (monitor_host.get("volumes") or {})
    percents = [v.get("used_percent") for v in vols.values()
                if isinstance(v, dict) and v.get("used_percent") is not None]
    verdict("monitor_volumes",
            any(p > thresholds["platform_volume_warn_percent"] for p in percents),
            known=bool(percents))
    verdict("monitor_services",
            not ((monitor_host.get("asgi") or {}).get("active")
                 and (monitor_host.get("ops_agent") or {}).get("active")),
            known=True)
    verdicts["reporter"] = ("ok" if reporter_status == "fresh"
                            else "warning" if reporter_status == "stale"
                            else "unknown")
    if any(v == "warning" for v in verdicts.values()):
        overall = "warning"
    elif all(v == "ok" for v in verdicts.values()):
        overall = "ok"
    else:
        overall = "ok" if any(v == "ok" for v in verdicts.values()) else "unknown"
    return {"overall": overall, "verdicts": verdicts,
            "thresholds": thresholds}


# ── Projection and publication ──────────────────────────────────────────

def _previous_source_time():
    from snapper_ai.models import CurrentComponent

    row = (CurrentComponent.objects
           .filter(scope=SCOPE, name=COMPONENT_NAME)
           .values("source_as_of").first())
    return row["source_as_of"] if row else None


def platform_projection(now=None, mark=None):
    """Build the platform projection without publishing. The interval
    is (mark, now]; mark defaults to the component's current source
    time, or a short first interval for a fresh record."""
    observed_at = now or timezone.now()
    if mark is None:
        mark = _previous_source_time()
    if mark is None or mark >= observed_at:
        mark = observed_at - timedelta(minutes=FIRST_INTERVAL_MINUTES)
    thresholds = {key: _config(key) for key in CONFIG_DEFAULTS
                  if key.startswith("platform_") and (
                      "warn" in key)}
    database = database_reading()
    heartbeats = heartbeat_reading(
        mark, observed_at, int(_config("platform_heartbeat_period_seconds")))
    server = server_reading(str(_config("platform_panda_server_url")),
                            _config("platform_server_timeout_seconds"))
    pandamon = pandamon_reading(str(_config("platform_pandamon_url")),
                                _config("platform_pandamon_timeout_seconds"),
                                observed_at)
    server_host, reporter_status = server_host_reading(
        observed_at, int(_config("platform_reporter_stale_seconds")))
    monitor_host = monitor_host_reading(
        list(_config("platform_monitor_volumes") or []))
    projection = {
        "interval": {"start": _iso_utc(mark), "end": _iso_utc(observed_at)},
        "database": database,
        "heartbeats": heartbeats,
        "server": server,
        "pandamon": pandamon,
        "reporter_status": reporter_status,
        "monitor_host": monitor_host,
        "assessment": assess(database, heartbeats, server, reporter_status,
                             monitor_host, thresholds, pandamon),
    }
    if server_host is not None:
        projection["server_host"] = server_host
    serialized = len(json.dumps(projection, separators=(",", ":"), default=str))
    if serialized > MAX_SERIALIZED_BYTES:
        raise ValueError(
            f"platform projection serializes to {serialized} bytes, over "
            f"the {MAX_SERIALIZED_BYTES} bound")
    return projection, observed_at


def publish_platform_state() -> PlatformPublication:
    """Measure, assess, and atomically publish the platform component.
    Publication is unconditional: the gauges change every interval."""
    projection, observed_at = platform_projection()
    with transaction.atomic():
        registration_update = register_component(
            scope=SCOPE,
            name=COMPONENT_NAME,
            publisher_identity=PUBLISHER_IDENTITY,
            registration=PLATFORM_REGISTRATION,
            component_schema_version=1,
        )
        update = publish_component(
            scope=SCOPE,
            name=COMPONENT_NAME,
            publisher_identity=PUBLISHER_IDENTITY,
            data=projection,
            assessed_at=observed_at,
            source_as_of=observed_at,
            assessment_policy_version=ASSESSMENT_POLICY_VERSION,
        )
    return PlatformPublication(
        registration_update=registration_update,
        update=update,
        projection=projection,
        observed_at=observed_at,
    )


def compact_platform_publication_report(publication: PlatformPublication) -> str:
    projection = publication.projection
    heartbeats = projection["heartbeats"]
    return json.dumps(
        {
            "scope": SCOPE,
            "component": COMPONENT_NAME,
            "revision": max(publication.update.revision,
                            publication.registration_update.revision),
            "content_changed": publication.update.content_changed,
            "interval": projection["interval"],
            "db_connections": projection["database"].get("connections"),
            "running": heartbeats.get("running"),
            "heartbeats_received": heartbeats.get("received"),
            "heartbeat_yield": heartbeats.get("yield"),
            "server_latency_ms": projection["server"].get("latency_ms"),
            "server_ok": projection["server"].get("ok"),
            "pandamon_front_ms": projection["pandamon"]["front"].get("latency_ms"),
            "pandamon_workers_ms": projection["pandamon"]["workers"].get("latency_ms"),
            "reporter_status": projection["reporter_status"],
            "assessment": projection["assessment"]["overall"],
            "observed_at": publication.observed_at.isoformat(),
        },
        indent=2,
        sort_keys=True,
    )
