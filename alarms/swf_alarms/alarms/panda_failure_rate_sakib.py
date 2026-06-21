"""Alarm: panda_failure_rate_sakib.

Alerts on PanDA tasks owned by Sakib Rahman whose computed failure rate
exceeds the configured threshold over the configured lookback. Delegates
the actual math to the shared task-failure-rate helper, which walks
PanDA's task REST endpoint.

Free to diverge later (owner-specific subject line, etc.) — that's the
point of the snowflake layout.
"""
from __future__ import annotations

from ..lib.failure_rate import task_failure_rate, PARAMS as _FR_PARAMS


# This alarm's public param surface — same as the shared helper today,
# but it's declared here so the editor's help panel can render it per
# alarm (different alarms can declare different surfaces).
PARAMS = dict(_FR_PARAMS)


def detect(client, params):
    yield from task_failure_rate(client, params)
