"""Alarm: harvester_contact.

Every harvester advances its instance record in PanDA
(``harvester_instances.lastupdate``) on each call it makes to the server.
A harvester that cannot reach the server, for any reason, stops advancing
it, and its queues get no work. This alarm reads that record for each
watched instance and fires when it is older than the limit.

From 2026-10-02 14:22 ET every harvester failed to verify the PanDA server's
new certificate chain; the queues read only as idle, and three harvesters
stayed cut off for 29 hours after the first was fixed. Read from PanDA's own
record, the outage would have shown here for all of them within the limit.
"""
from __future__ import annotations

from ..common import Detection

PARAMS = {
    # harvester_id values, comma separated: the instances ePIC production uses.
    "instances": "BNL_harvester_1, BNL_harvester_2, BNL_osg_harvester_1, Perlmutter_test_1",
    # A working harvester reaches the server every few minutes.
    "max_age_minutes": 30,
}


def detect(client, params):
    watched = [i.strip() for i in str(params.get("instances", "")).split(",") if i.strip()]
    limit = int(params.get("max_age_minutes", 30))
    data = client.harvester_instances()
    known = {row.get("harvester_id"): row for row in data.get("instances") or []}
    for instance in watched:
        row = known.get(instance)
        if row is None:
            yield Detection(
                dedupe_key=f"harvester:{instance}:unknown",
                subject=f"harvester {instance} has no instance record in PanDA",
                body_context=f"PanDA's harvester_instances has no row for {instance}.",
                extra_data={"instance": instance},
            )
            continue
        age = row.get("age_s")
        if age is None or age > limit * 60:
            shown = "never" if age is None else f"{age / 3600:.1f} h ago" if age >= 7200 else f"{age // 60} min ago"
            yield Detection(
                dedupe_key=f"harvester:{instance}:silent",
                subject=f"harvester {instance} ({row.get('hostname') or 'no host'}) last reached PanDA {shown}",
                body_context=(
                    f"The harvester {instance} on {row.get('hostname') or 'an unrecorded host'} last "
                    f"reached the PanDA server at {row.get('lastupdate')} UTC (limit {limit} min). "
                    "Its queues get no new work until it does. Its fetcher and communicator logs "
                    "name the failing call."),
                extra_data={"instance": instance, "hostname": row.get("hostname"),
                            "lastupdate": str(row.get("lastupdate")), "age_s": age},
            )
