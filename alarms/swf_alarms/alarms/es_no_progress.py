"""Alarm: es_no_progress.

An Event Service job whose node is lost is closed out by the production
operations agent (swf-epicprod es_closeout.py) and finished: a lost node is
not a failure of the job. A file whose jobs lose their node several times in
a row with nothing credited is the exception, since a job that takes its own
node down (memory, disk) looks like a node loss on every pass. At
``es_closeout.no_progress_limit`` such losses in a row the close-out sends
the job failed and records an ``es_no_progress_limit`` action; this alarm
raises one event per file so a person reads the jobs' records
(swf-epicprod docs/NODE_EVENT_DISPATCHER.md, Preemption).
"""
from __future__ import annotations

from ..common import Detection

PARAMS = {
    # How long a file stays raised after its last limit action.
    "window_hours": 24,
}


def detect(client, params):
    window = int(params.get("window_hours", 24))
    with client.db_conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp, message, extra_data FROM swf_applog "
            "WHERE app_name = 'epicprod' AND extra_data->>'action' = 'es_no_progress_limit' "
            "AND timestamp >= now() - make_interval(hours => %s) ORDER BY id DESC",
            [window])
        rows = cur.fetchall()
    seen = set()
    for row in rows:
        x = row["extra_data"] or {}
        key = x.get("subject_key") or f"{x.get('jeditaskid')}:{x.get('fileid')}"
        if key in seen:
            continue
        seen.add(key)
        yield Detection(
            dedupe_key=f"es_no_progress:{key}",
            subject=(f"Event Service file {x.get('fileid')} of task {x.get('jeditaskid')}: "
                     f"{x.get('streak')} jobs in a row lost their node with no progress"),
            body_context=(
                f"{row['message'] or ''}\n\n"
                "Genuine node losses (spot preemption) are random; the same file losing its "
                "node before any close stands, pass after pass, suggests the job itself takes "
                "the node down. Read the jobs' Event Service records and payload reports, and "
                "the cluster's node events for the nodes named."),
            extra_data={"jeditaskid": x.get("jeditaskid"), "fileid": x.get("fileid"),
                        "streak": x.get("streak"), "jobs": x.get("jobs"),
                        "at": str(row["timestamp"])},
        )
