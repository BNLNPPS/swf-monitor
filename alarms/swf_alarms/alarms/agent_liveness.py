"""Alarm: agent_liveness.

The production operations agent and the canary agent execute what operators
and crons ask of them; a request that reaches neither is lost without a
word. This alarm fires for each watched agent type when no instance has
heartbeated within the maximum age, and when the type has started more
instances in the window than a healthy unit does, which is a restart loop.

On 2026-10-02 the prod-ops agent restarted every two minutes from 10:59 to
11:18 ET, unable to reach the monitor over TLS, and an operator's pause
batch was lost in the gap; the watchdog's restarts looked like routine.

A restart loop counts only instances that ended without a clean exit. A
deploy stops each instance cleanly (status EXITED) and starts the next, so
deploys never count, however many land in the window: on 2026-10-28 four
deploys in 25 minutes raised the alarm for healthy agents, a false
positive (Torre Wenaus: "obv a false positive to be prevented").
"""
from __future__ import annotations

from ..common import Detection

PARAMS = {
    # agent_type values in swf_systemagent, comma separated.
    "agent_types": "PRODOPS,CANARY",
    # A healthy agent heartbeats every minute.
    "max_heartbeat_age_minutes": 5,
    # Instances created in the window that ended without a clean exit
    # (not EXITED), the running one aside; a deploy's restart exits cleanly.
    "restart_window_minutes": 15,
    "max_instances_in_window": 2,
}


def detect(client, params):
    types = [t.strip() for t in str(params.get("agent_types", "")).split(",") if t.strip()]
    max_age = int(params.get("max_heartbeat_age_minutes", 5))
    window = int(params.get("restart_window_minutes", 15))
    max_instances = int(params.get("max_instances_in_window", 2))
    with client.db_conn.cursor() as cur:
        for agent_type in types:
            cur.execute(
                "SELECT instance_name, status, operational_state, last_heartbeat, "
                "EXTRACT(EPOCH FROM now() - last_heartbeat) / 60.0 AS age_min "
                "FROM swf_systemagent WHERE agent_type = %s "
                "ORDER BY last_heartbeat DESC NULLS LAST LIMIT 1", [agent_type])
            newest = cur.fetchone()
            cur.execute(
                "SELECT count(*) AS n FROM swf_systemagent WHERE agent_type = %s "
                "AND created_at >= now() - make_interval(mins => %s) "
                "AND status <> 'EXITED' AND instance_name <> %s",
                [agent_type, window, (newest or {}).get("instance_name") or ""])
            unclean = int(cur.fetchone()["n"])

            if newest is None or newest["last_heartbeat"] is None or float(newest["age_min"]) > max_age:
                age = "never" if newest is None or newest["age_min"] is None else f"{float(newest['age_min']):.0f} min"
                yield Detection(
                    dedupe_key=f"agent:{agent_type}:silent",
                    subject=f"{agent_type} agent silent: last heartbeat {age} ago (max {max_age} min)",
                    body_context=(
                        f"No {agent_type} agent instance has heartbeated in {max_age} minutes. "
                        "Requests published to it are not being executed. Check "
                        "journalctl -u epicprod-ops-agent / canary-agent for the failure."),
                    extra_data={"agent_type": agent_type, "max_age_minutes": max_age,
                                "newest_instance": newest and newest["instance_name"],
                                "newest_heartbeat": newest and str(newest["last_heartbeat"])},
                )
            if unclean >= max_instances:
                yield Detection(
                    dedupe_key=f"agent:{agent_type}:restarting",
                    subject=(f"{agent_type} agent restart loop: {unclean} instances ended "
                             f"without a clean exit in {window} min"),
                    body_context=(
                        f"{unclean} {agent_type} agent instances started in the last {window} minutes "
                        "ended without a clean exit (a deploy's restart exits cleanly and is not "
                        "counted). The agent is failing and the watchdog keeps restarting it; "
                        "requests sent meanwhile are lost."),
                    extra_data={"agent_type": agent_type, "instances": unclean,
                                "window_minutes": window},
                )
