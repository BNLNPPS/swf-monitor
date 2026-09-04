"""Alarm: pings.

A ping is a dated obligation entered on the alarm dashboard or accepted
there from an AI proposal (docs/PINGS.md). This module carries every open
ping through the engine's tick: a ping whose due date is within its lead
time is detected at severity ``ping``; past its due date it is detected
at severity ``alarm``; a fulfilled ping is no longer detected and its
event clears. Nothing here observes fulfilment: a person records it.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .. import db
from ..common import Detection

PARAMS = {
    # Lead time for pings that declare none.
    "default_lead_days": 7,
}

_EASTERN = ZoneInfo("America/New_York")


def detect(client, params):
    default_lead = int(params.get("default_lead_days", 7))
    today = datetime.now(_EASTERN).date()
    for row in db.list_open_pings(client.db_conn):
        data = row.get("data") or {}
        title = (row.get("title") or "").strip() or "(untitled ping)"
        key = f"ping:{row['id']}"
        try:
            due = date.fromisoformat(str(data.get("due") or "")[:10])
        except ValueError:
            yield Detection(
                dedupe_key=key,
                subject=f"ping without a valid due date: {title}",
                body_context=(f"The ping's due field is {data.get('due')!r}; "
                              "set a date on the alarm dashboard."),
                extra_data={"severity": "alarm", "ping_id": row["id"],
                            "owner": data.get("owner") or ""},
            )
            continue
        lead = int(data.get("lead_days") or default_lead)
        days_left = (due - today).days
        if days_left > lead:
            continue
        overdue = days_left < 0
        if overdue:
            when = f"overdue by {-days_left} day{'s' if days_left != -1 else ''}"
        elif days_left == 0:
            when = "due today"
        else:
            when = f"due in {days_left} day{'s' if days_left != 1 else ''}"
        lines = [(row.get("content") or "").strip()]
        if data.get("owner"):
            lines.append(f"Owner: {data['owner']}")
        if data.get("url"):
            lines.append(f"See: {data['url']}")
        lines.append("Mark the ping fulfilled on the alarm dashboard once the "
                     "obligation is met; the event clears on the next tick.")
        yield Detection(
            dedupe_key=key,
            subject=f"{when} ({due.isoformat()}): {title}",
            body_context="\n".join(line for line in lines if line),
            extra_data={
                "severity": "alarm" if overdue else "ping",
                "ping_id": row["id"],
                "due": due.isoformat(),
                "days_left": days_left,
                "owner": data.get("owner") or "",
                "url": data.get("url") or "",
                "origin": data.get("origin") or "manual",
            },
        )
