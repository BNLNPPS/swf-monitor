"""Alarm: panda_failure_rate_eic_all.

Catch-all alert on any PanDA task whose computed failure rate exceeds the
configured threshold over the configured lookback. Tuning channel for
shaping future per-owner alarms.
"""
from __future__ import annotations

from ..lib.failure_rate import task_failure_rate, PARAMS as _FR_PARAMS


PARAMS = dict(_FR_PARAMS)


def detect(client, params):
    yield from task_failure_rate(client, params)
