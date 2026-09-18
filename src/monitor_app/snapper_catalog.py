"""The JLab Rucio catalog component (docs/SNAPPER_CATALOG.md): what the
production jobs asked of the catalog of record, what they got, and how
the catalog was answering, published every five minutes beside the
platform component on the System-status refresh.

Four groups:

- probe: timed from this host, a TLS handshake with the Rucio server,
  its /ping, and one authenticated light read; a timeout is recorded
  at the timeout value with its reason, never omitted.
- registrations: over the half-open interval since the previous
  publication, every production job that ended, read from the digest
  the payload writes into the PanDA job-metrics string on every job
  whatever its end (payloadRegistration, payloadExit, payloadVersion);
  counts by outcome, the failed split by payload exit code, per queue.
- latency: the registration stage's wall seconds over the interval's
  jobs whose payload report is at hand (the metatable for finished
  jobs, the swept reports for failed ones); count, median, p90.
- registrar: pending registrations owed over the trailing day, from
  the same digest.

Publication follows the platform component: every interval, since the
probe is a gauge that moves every time.
"""
import json
import logging
import re
import socket
import ssl
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from urllib.parse import urlparse

from django.db import connections, transaction
from django.utils import timezone

from snapper_ai.services import (
    ComponentUpdate,
    publish_component,
    register_component,
)

from .panda.constants import PANDA_SCHEMA

logger = logging.getLogger(__name__)

PUBLISHER_IDENTITY = "swf-monitor:jlab-catalog"
ASSESSMENT_POLICY_VERSION = "swf-jlab-catalog-v1"
COMPONENT_SCHEMA_VERSION = 1
COMPONENT_NAME = "catalog"
SCOPE = "epicprod"
MAX_SERIALIZED_BYTES = 64 * 1024
MAX_QUEUES = 32
FIRST_INTERVAL_MINUTES = 5
REGISTRAR_WINDOW_HOURS = 24

# The digest's registration outcomes as the payload writes them
# (payload_report.registration_record), in the order the view stacks
# them. 'unfinished' is a job that died inside its registration stage or
# stashed its output at BNL without closing the stage.
OUTCOMES = ("registered", "pending", "diverted", "unfinished", "failed", "not_reached", "none")

CONFIG_DEFAULTS = {
    "catalog_rucio_url": "https://rucio-server.jlab.org:443",
    "catalog_probe_timeout_s": 10,
    "catalog_probe_did": "epic:/RECO/26.07.1/epic_craterlake/DIS/DJANGOH4.6.10-2.0/CC/Rad/eMinus-pPlus/9x275/q2_3000to9000",
    "catalog_probe_slow_ms": 2000,
    "catalog_failed_share": 0.10,
    "catalog_failed_floor": 20,
    "catalog_pending_old_hours": 6,
}

CATALOG_REGISTRATION = {
    "title": "JLab Rucio catalog",
    "description": (
        "Five-minute readings of the JLab Rucio catalog of record: a timed "
        "probe from the monitor host (TLS, /ping, one authenticated read), "
        "the interval's production jobs by registration outcome from the "
        "payload digest every job carries (registered, pending, failed by "
        "payload exit code, not reached), the registration stage's wall "
        "time from the payload reports at hand, and the pending "
        "registrations owed over the trailing day."
    ),
    "visibility": "public",
    "owning_subsystem": "SWF PanDA production monitor",
    "assessment_policy": ASSESSMENT_POLICY_VERSION,
    "max_serialized_bytes": MAX_SERIALIZED_BYTES,
    "quantities": {
        "interval": {
            "path": "interval", "type": "object", "required": True, "kind": "window",
            "description": "The half-open interval (start, end] the registrations and latency groups cover.",
        },
        "probe": {
            "path": "probe", "type": "object", "required": True, "kind": "gauge",
            "description": (
                "Timed from the monitor host: tls (handshake), ping (GET /ping), "
                "read (one authenticated DID metadata read), each {latency_ms, ok, "
                "timeout, error}; a timeout is recorded at the timeout value."
            ),
        },
        "registrations": {
            "path": "registrations", "type": "object", "required": True, "kind": "interval",
            "description": (
                "Production jobs ended in the interval by the digest's registration "
                "outcome (registered, pending, diverted, start, failed, not_reached, "
                "none), the failed split by payload exit code, per queue (bounded), "
                "and the total; from jobmetrics on jobsactive4 and jobsarchived4."
            ),
        },
        "latency": {
            "path": "latency", "type": "object", "required": True, "kind": "interval",
            "description": (
                "The registration stage's wall seconds over the interval's jobs whose "
                "payload report is at hand: count, median_s, p90_s, and the same for "
                "the registered jobs alone; absent counts read as no report."
            ),
        },
        "registrar": {
            "path": "registrar", "type": "object", "required": True, "kind": "gauge",
            "description": "Pending registrations owed over the trailing day and the oldest pending job's age in hours, from the digest.",
        },
        "assessment": {
            "path": "assessment", "type": "object", "required": True, "kind": "assessment",
            "description": "Verdicts on the probe, the failed share of registrations and the pending backlog, with the thresholds, and the overall.",
        },
    },
}


@dataclass
class CatalogPublication:
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


# ── Probe ──────────────────────────────────────────────────────────────

def _timed(label, fn, timeout_seconds):
    started = time.monotonic()
    try:
        detail = fn()
        ms = round((time.monotonic() - started) * 1000, 1)
        return {"latency_ms": ms, "ok": True, "timeout": False, **(detail or {})}
    except (socket.timeout, TimeoutError) as e:
        return {"latency_ms": round(float(timeout_seconds) * 1000, 1), "ok": False,
                "timeout": True, "error": f"no response within {timeout_seconds}s ({e})"[:200]}
    except Exception as e:                                        # noqa: BLE001
        elapsed = round((time.monotonic() - started) * 1000, 1)
        is_timeout = "timed out" in str(e).lower() or "timeout" in type(e).__name__.lower()
        return {"latency_ms": round(float(timeout_seconds) * 1000, 1) if is_timeout else elapsed,
                "ok": False, "timeout": is_timeout,
                "error": f"{type(e).__name__}: {e}"[:200]}


def probe_reading(rucio_url, timeout_seconds, did):
    """The three timings against the catalog, from this host."""
    parsed = urlparse(rucio_url if "://" in rucio_url else f"https://{rucio_url}")
    host = parsed.hostname or rucio_url
    port = parsed.port or 443

    def tls():
        with socket.create_connection((host, port), timeout=float(timeout_seconds)) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as s:
                s.settimeout(float(timeout_seconds))
                return {"peer": host}

    def ping():
        import requests
        r = requests.get(f"{parsed.scheme or 'https'}://{host}:{port}/ping",
                         timeout=float(timeout_seconds))
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return {"status": r.status_code}

    def read():
        # The production account through the proxy the ops agent holds
        # (EVGEN_X509_PROXY, the registrar's credential); without one the
        # read is recorded as not made, never as the catalog's fault.
        import os
        from rucio.client import Client
        proxy = os.environ.get("EVGEN_X509_PROXY", "")
        if not proxy or not os.path.exists(proxy):
            raise RuntimeError("no proxy (EVGEN_X509_PROXY) on this host; read not made")
        scope, name = did.split(":", 1)
        base = f"{parsed.scheme or 'https'}://{host}:{port}"
        client = Client(rucio_host=base, auth_host=base, account="eicprod",
                        auth_type="x509_proxy", creds={"client_proxy": proxy},
                        timeout=float(timeout_seconds))
        meta = client.get_metadata(scope, name)
        return {"did": did, "found": bool(meta)}

    return {"host": host, "port": port, "timeout_s": float(timeout_seconds),
            "tls": _timed("tls", tls, timeout_seconds),
            "ping": _timed("ping", ping, timeout_seconds),
            "read": _timed("read", read, timeout_seconds)}


# ── Registrations from the digest ──────────────────────────────────────

_REG_RE = re.compile(r"payloadRegistration=([A-Za-z_]+)")
_EXIT_RE = re.compile(r"payloadExit=(-?\d+)")
_VERSION_RE = re.compile(r"payloadVersion=([0-9.]+)")


def parse_digest(jobmetrics):
    """(outcome, exit code or None, payload version or '') from a job's
    metrics string; ('none', None, '') when the digest is absent."""
    text = jobmetrics or ""
    m = _REG_RE.search(text)
    outcome = m.group(1) if m else "none"
    if outcome not in OUTCOMES:
        outcome = "none" if outcome in ("", "None") else outcome
    e = _EXIT_RE.search(text)
    v = _VERSION_RE.search(text)
    return outcome, (int(e.group(1)) if e else None), (v.group(1) if v else "")


def _ended_jobs(mark, until):
    """(computingsite, jobstatus, jobmetrics) of production jobs that
    ended in (mark, until], from both job tables."""
    rows = []
    sql = (f'SELECT "computingsite", "jobstatus", "jobmetrics" FROM "{PANDA_SCHEMA}"."{{table}}" '
           f'WHERE "processingtype" = %s AND "jobstatus" IN (%s, %s) '
           f'AND "endtime" > %s AND "endtime" <= %s')
    with connections["panda"].cursor() as cur:
        for table in ("jobsactive4", "jobsarchived4"):
            cur.execute(sql.format(table=table),
                        ["epicproduction", "finished", "failed", _naive_utc(mark), _naive_utc(until)])
            rows.extend(cur.fetchall())
    return rows


def registrations_from_rows(rows):
    """The registrations group from (site, status, jobmetrics) rows."""
    by_outcome = {o: 0 for o in OUTCOMES}
    failed_by_exit = {}
    by_queue = {}
    versions = {}
    for site, status, metrics in rows:
        outcome, exit_code, version = parse_digest(metrics)
        if outcome not in by_outcome:
            by_outcome[outcome] = 0
        by_outcome[outcome] += 1
        if outcome == "failed":
            key = str(exit_code if exit_code is not None else "unknown")
            failed_by_exit[key] = failed_by_exit.get(key, 0) + 1
        if version:
            versions[version] = versions.get(version, 0) + 1
        q = by_queue.setdefault(site or "unknown", {o: 0 for o in OUTCOMES})
        q[outcome] = q.get(outcome, 0) + 1
    if len(by_queue) > MAX_QUEUES:
        ranked = sorted(by_queue.items(), key=lambda kv: -sum(kv[1].values()))
        kept = dict(ranked[:MAX_QUEUES])
        other = {o: 0 for o in OUTCOMES}
        for _, counts in ranked[MAX_QUEUES:]:
            for o, n in counts.items():
                other[o] = other.get(o, 0) + n
        kept["other"] = other
        by_queue = kept
    total = sum(by_outcome.values())
    attempted = total - by_outcome.get("not_reached", 0) - by_outcome.get("none", 0)
    failed = by_outcome.get("failed", 0)
    return {
        "jobs": total,
        "attempted": attempted,
        "by_outcome": by_outcome,
        "failed_by_exit": dict(sorted(failed_by_exit.items())),
        "failed_share": round(failed / attempted, 4) if attempted else None,
        "by_queue": by_queue,
        "payload_versions": dict(sorted(versions.items())),
    }


def registrations_reading(mark, until):
    try:
        return registrations_from_rows(_ended_jobs(mark, until))
    except Exception as e:                                        # noqa: BLE001
        logger.error("catalog registrations read failed: %s", e)
        return {"error": f"{type(e).__name__}: {e}"[:300]}


# ── Latency from the payload reports ───────────────────────────────────

def _registration_wall(report):
    """The registration stage's wall seconds and outcome from a payload
    report, or (None, '') when the report has none."""
    stages = ((report or {}).get("stages") or {})
    reg = stages.get("registration") or {}
    wall = reg.get("wall_s")
    outcome = ((report or {}).get("registration") or {}).get("outcome") or reg.get("status") or ""
    try:
        return (float(wall) if wall is not None else None), str(outcome)
    except (TypeError, ValueError):
        return None, str(outcome)


def _latency_rows(mark, until):
    """[(wall_s, outcome)] over the interval's jobs with a report: the
    metatable for finished jobs, the swept reports for failed ones."""
    out = []
    sql = (f'SELECT m."metadata" FROM "{PANDA_SCHEMA}"."metatable" m '
           f'JOIN "{PANDA_SCHEMA}"."jobsarchived4" j ON j."pandaid" = m."pandaid" '
           f'WHERE j."processingtype" = %s AND j."endtime" > %s AND j."endtime" <= %s')
    with connections["panda"].cursor() as cur:
        cur.execute(sql, ["epicproduction", _naive_utc(mark), _naive_utc(until)])
        for (raw,) in cur.fetchall():
            try:
                meta = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                continue
            wall, outcome = _registration_wall((meta or {}).get("payload"))
            if wall is not None:
                out.append((wall, outcome))
    from .models import EpicProdJob
    for job in (EpicProdJob.objects.filter(updated_at__gt=mark, updated_at__lte=until + timedelta(hours=2))
                .only("data").iterator()):
        report = ((job.data or {}).get("payload_report") or {}).get("report")
        wall, outcome = _registration_wall(report)
        if wall is not None:
            out.append((wall, outcome))
    return out


def latency_from_rows(rows):
    def stats(values):
        if not values:
            return {"count": 0, "median_s": None, "p90_s": None}
        values = sorted(values)
        p90 = values[min(len(values) - 1, int(round(0.9 * (len(values) - 1))))]
        return {"count": len(values), "median_s": round(statistics.median(values), 1),
                "p90_s": round(p90, 1), "max_s": round(values[-1], 1)}
    return {"all": stats([w for w, _ in rows]),
            "registered": stats([w for w, o in rows if o == "registered"])}


def latency_reading(mark, until):
    try:
        return latency_from_rows(_latency_rows(mark, until))
    except Exception as e:                                        # noqa: BLE001
        logger.error("catalog latency read failed: %s", e)
        return {"error": f"{type(e).__name__}: {e}"[:300]}


# ── Registrar backlog ──────────────────────────────────────────────────

def registrar_reading(until):
    """Pending registrations owed over the trailing day, from the
    digest, with the oldest pending job's age."""
    since = until - timedelta(hours=REGISTRAR_WINDOW_HOURS)
    sql = (f'SELECT "endtime" FROM "{PANDA_SCHEMA}"."{{table}}" '
           f'WHERE "processingtype" = %s AND "endtime" > %s AND "endtime" <= %s '
           f'AND "jobmetrics" LIKE %s')
    try:
        ends = []
        with connections["panda"].cursor() as cur:
            for table in ("jobsactive4", "jobsarchived4"):
                cur.execute(sql.format(table=table),
                            ["epicproduction", _naive_utc(since), _naive_utc(until),
                             "%payloadRegistration=pending%"])
                ends.extend(r[0] for r in cur.fetchall() if r[0] is not None)
        oldest_h = None
        if ends:
            oldest = min(ends)
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=dt_timezone.utc)
            oldest_h = round((until - oldest).total_seconds() / 3600, 1)
        return {"window_hours": REGISTRAR_WINDOW_HOURS, "pending": len(ends),
                "oldest_pending_h": oldest_h}
    except Exception as e:                                        # noqa: BLE001
        logger.error("catalog registrar read failed: %s", e)
        return {"error": f"{type(e).__name__}: {e}"[:300]}


# ── Assessment ─────────────────────────────────────────────────────────

def assess(probe, registrations, registrar, thresholds):
    verdicts = {}

    def verdict(name, warning, known=True):
        verdicts[name] = "warning" if (known and warning) else ("ok" if known else "unknown")

    slow_ms = float(thresholds["catalog_probe_slow_ms"])
    probe_known = bool(probe) and "error" not in probe
    probe_bad = probe_known and any(
        (not (probe.get(k) or {}).get("ok")) or float((probe.get(k) or {}).get("latency_ms") or 0) > slow_ms
        for k in ("tls", "ping", "read"))
    verdict("probe", probe_bad, probe_known)
    reg_known = bool(registrations) and "error" not in registrations
    share = (registrations or {}).get("failed_share")
    attempted = int((registrations or {}).get("attempted") or 0)
    verdict("failed_share",
            reg_known and share is not None and attempted >= int(thresholds["catalog_failed_floor"])
            and share >= float(thresholds["catalog_failed_share"]),
            reg_known)
    backlog_known = bool(registrar) and "error" not in registrar
    oldest = (registrar or {}).get("oldest_pending_h")
    verdict("pending_backlog",
            backlog_known and oldest is not None and oldest >= float(thresholds["catalog_pending_old_hours"]),
            backlog_known)
    overall = ("warning" if "warning" in verdicts.values()
               else "ok" if all(v == "ok" for v in verdicts.values()) else "unknown")
    return {"overall": overall, "verdicts": verdicts, "thresholds": thresholds}


# ── Projection and publication ─────────────────────────────────────────

def _previous_publication():
    from snapper_ai.models import CurrentComponent

    row = (CurrentComponent.objects.filter(scope=SCOPE, name=COMPONENT_NAME)
           .values("source_as_of").first())
    return row["source_as_of"] if row else None


def catalog_projection(now=None, mark=None, probe=True):
    observed_at = now or timezone.now()
    if mark is None:
        mark = _previous_publication()
    if mark is None or mark >= observed_at:
        mark = observed_at - timedelta(minutes=FIRST_INTERVAL_MINUTES)
    thresholds = {k: _config(k) for k in ("catalog_probe_timeout_s", "catalog_probe_slow_ms",
                                          "catalog_failed_share", "catalog_failed_floor",
                                          "catalog_pending_old_hours")}
    probe_reading_ = (probe_reading(str(_config("catalog_rucio_url")),
                                    _config("catalog_probe_timeout_s"),
                                    str(_config("catalog_probe_did")))
                      if probe else {"error": "not probed"})
    registrations = registrations_reading(mark, observed_at)
    latency = latency_reading(mark, observed_at)
    registrar = registrar_reading(observed_at)
    projection = {
        "interval": {"start": _iso_utc(mark), "end": _iso_utc(observed_at)},
        "probe": probe_reading_,
        "registrations": registrations,
        "latency": latency,
        "registrar": registrar,
        "assessment": assess(probe_reading_, registrations, registrar, thresholds),
    }
    serialized = len(json.dumps(projection, separators=(",", ":"), default=str))
    if serialized > MAX_SERIALIZED_BYTES:
        raise ValueError(f"catalog projection serializes to {serialized} bytes, over "
                         f"the {MAX_SERIALIZED_BYTES} bound")
    return projection, observed_at


def publish_catalog_state() -> CatalogPublication:
    """Measure, assess, and atomically publish the catalog component."""
    projection, observed_at = catalog_projection()
    with transaction.atomic():
        registration_update = register_component(
            scope=SCOPE, name=COMPONENT_NAME, publisher_identity=PUBLISHER_IDENTITY,
            registration=CATALOG_REGISTRATION,
            component_schema_version=COMPONENT_SCHEMA_VERSION)
        update = publish_component(
            scope=SCOPE, name=COMPONENT_NAME, publisher_identity=PUBLISHER_IDENTITY,
            data=projection, assessed_at=observed_at, source_as_of=observed_at,
            assessment_policy_version=ASSESSMENT_POLICY_VERSION)
    return CatalogPublication(registration_update=registration_update, update=update,
                              projection=projection, observed_at=observed_at)


def compact_catalog_publication_report(publication: CatalogPublication) -> str:
    p = publication.projection
    probe = p.get("probe") or {}
    reg = p.get("registrations") or {}
    by = reg.get("by_outcome") or {}
    timings = " ".join(f"{k} {((probe.get(k) or {}).get('latency_ms'))}ms"
                       + ("" if (probe.get(k) or {}).get("ok") else "!")
                       for k in ("tls", "ping", "read")) if "error" not in probe else probe["error"]
    return (f"catalog: {p['assessment']['overall']}; probe {timings}; "
            f"registrations {reg.get('jobs', '?')} jobs: registered {by.get('registered', 0)}, "
            f"pending {by.get('pending', 0)}, failed {by.get('failed', 0)} {reg.get('failed_by_exit') or ''}; "
            f"pending owed {((p.get('registrar') or {}).get('pending'))}")
