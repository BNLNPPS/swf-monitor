"""Shared task-failure-rate helper.

Yields one Detection per PanDA task whose final-failure rate exceeds the
configured threshold. Uses `computed_finalfailurerate` (nfinalfailed /
(nfinalfailed + nfinished)) — nfinalfailed counts only jobs failed with
attemptnr >= maxattempt (3), i.e. retry-exhausted true failures. Jobs
that failed once or twice and succeeded on retry don't count.

Rationale (Rahman, NPPS 2026-04-22): nfailed counts every failed job
record including retries that later succeeded, which inflates the rate
and pages on noise. Alarms should trigger only on true failures.

Falls back to `computed_failurerate` (all-failures rate) with a stderr
warning if the upstream swf-monitor doesn't yet expose the new field —
covers the window while swf-monitor is redeployed.

Alarm modules import and delegate to `task_failure_rate(client, params)`.
No central registry.

Params (read from the alarm config entry's data.params):

  threshold          float, required. e.g. 0.03 for 3%.
  since_days         int, default 1. How far back to look at PanDA data.
  username           str, optional (supports % LIKE wildcard upstream).
  processingtype     str, optional.
  min_terminal_jobs  int, default 5. Noise floor: tasks with fewer
                     terminal jobs (nfinalfailed + nfinished) are skipped.
  statuses           list[str], optional. Task statuses to consider;
                     defaults to ['running', 'failed', 'broken'].
"""
from __future__ import annotations

from . import Detection


DEFAULT_STATUSES = ["running", "failed", "broken"]

# Param schema consumed by the shared helper. Alarm modules may re-export
# or extend this. Each value is a dict with keys:
#   type          python type used for the param (float, int, str, list)
#   required      True if this param has no default and must be supplied
#   default       default value when the alarm config omits the key
#   description   human-readable one-liner for the editor help panel
PARAMS: dict[str, dict] = {
    "threshold": {
        "type": float, "required": True,
        "description": "failure-rate threshold (e.g. 0.03 = 3%)",
    },
    "since_days": {
        "type": int, "default": 1,
        "description": "look back this many days into PanDA",
    },
    "username": {
        "type": str,
        "description": "optional task-owner filter (supports % LIKE)",
    },
    "processingtype": {
        "type": str,
        "description": "optional PanDA processingtype filter",
    },
    "min_terminal_jobs": {
        "type": int, "default": 5,
        "description": "ignore tasks with fewer finished+failed jobs than this",
    },
    "statuses": {
        "type": list,
        "description": "task statuses to consider; default running/failed/broken",
    },
}


def task_failure_rate(client, params: dict):
    import logging
    _logger = logging.getLogger(__name__)
    _warned_fallback = False

    threshold = float(params["threshold"])
    since_days = int(params.get("since_days", 1))
    username = params.get("username")
    processingtype = params.get("processingtype")
    min_terminal = int(params.get("min_terminal_jobs", 5))
    statuses = params.get("statuses") or DEFAULT_STATUSES

    for status in statuses:
        for t in client.iter_all_tasks(
            days=since_days, status=status,
            username=username,
            processingtype=processingtype,
        ):
            # Prefer retry-exhausted-failures rate (true failures).
            # Fall back to all-failures rate only if upstream doesn't
            # expose the new field (stale swf-monitor).
            cfr = t.get("computed_finalfailurerate")
            using_finalrate = cfr is not None
            if not using_finalrate:
                cfr = t.get("computed_failurerate")
                if cfr is not None and not _warned_fallback:
                    _logger.warning(
                        "task_failure_rate: upstream swf-monitor lacks "
                        "computed_finalfailurerate; falling back to "
                        "computed_failurerate (all failures, not retry-"
                        "exhausted). Deploy swf-monitor to activate the "
                        "nfinalfailed-based trigger."
                    )
                    _warned_fallback = True
            if cfr is None:
                continue
            nfinished = int(t.get("nfinished") or 0)
            nfailed_all = int(t.get("nfailed") or 0)
            nfinalfailed = (
                int(t.get("nfinalfailed") or 0) if using_finalrate
                else nfailed_all
            )
            if nfinalfailed + nfinished < min_terminal:
                continue
            if cfr < threshold:
                continue

            jeditaskid = t["jeditaskid"]
            rate_label = (
                "final-failure rate" if using_finalrate
                else "failure rate (fallback — swf-monitor stale)"
            )
            yield Detection(
                dedupe_key=f"task:{jeditaskid}",
                subject=(
                    f"task {jeditaskid} ({t.get('status') or '?'}) "
                    f"{rate_label} {cfr*100:.1f}% — "
                    f"{t.get('taskname') or '?'}"
                ),
                body_context=_body_detail(
                    jeditaskid=jeditaskid,
                    taskname=t.get("taskname") or "?",
                    task_status=t.get("status") or "?",
                    task_user=t.get("username") or "?",
                    site=t.get("site") or "?",
                    cfr=cfr, nfailed=nfinalfailed, nfinished=nfinished,
                    nactive=int(t.get("nactive") or 0),
                    threshold=threshold, since_days=since_days,
                    native_failurerate=t.get("failurerate"),
                    rate_kind=rate_label,
                ),
                extra_data={
                    "metric": f"{cfr*100:.1f}%",
                    "rate_kind": (
                        "final-failure" if using_finalrate
                        else "all-failures-fallback"
                    ),
                    "jeditaskid": jeditaskid,
                    "taskname": t.get("taskname"),
                    "status": t.get("status"),
                    "username": t.get("username"),
                    "site": t.get("site"),
                    "computed_failurerate": t.get("computed_failurerate"),
                    "computed_finalfailurerate": t.get(
                        "computed_finalfailurerate"),
                    "native_failurerate": t.get("failurerate"),
                    "nactive": int(t.get("nactive") or 0),
                    "nfinished": nfinished,
                    "nfailed": nfailed_all,
                    "nfinalfailed": nfinalfailed,
                    "nretries": int(t.get("nretries") or 0),
                    "threshold": threshold,
                    "since_days": since_days,
                },
            )


def _body_detail(**k) -> str:
    native = k["native_failurerate"]
    native_line = (
        f"JEDI native failurerate: {native} (computed is the operative signal)"
        if native is not None
        else "JEDI native failurerate: NULL (expected — not populated for ePIC task types)"
    )
    rate_kind = k.get("rate_kind", "final-failure rate")
    return (
        f"PanDA task {k['jeditaskid']} — {k['taskname']}\n"
        f"Status:      {k['task_status']}\n"
        f"Owner:       {k['task_user']}\n"
        f"Site:        {k['site']}\n"
        f"\n"
        f"{rate_kind}: {k['cfr']*100:.1f}%  (threshold {k['threshold']*100:.1f}%)\n"
        f"Jobs: nfinalfailed={k['nfailed']}  nfinished={k['nfinished']}  nactive={k['nactive']}\n"
        f"(nfinalfailed = failed jobs with attemptnr >= maxattempt 3 — retry-exhausted true failures)\n"
        f"Since: last {k['since_days']} day(s)\n"
        f"{native_line}\n"
        f"\n"
        f"Task page: https://epic-devcloud.org/prod/panda/tasks/{k['jeditaskid']}/\n"
    )
