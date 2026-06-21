"""Shared helpers for alarm modules.

Nothing here is a registry or dispatch table — alarm modules import what
they need and no more. `Detection` is the value-type alarm modules yield;
subfiles (e.g. ``failure_rate.py``) are reusable PanDA-query helpers.

Snowflake rule (from the project dialog): each alarm is its own module
under ``swf_alarms/alarms/<name>.py`` that exposes ``check(client, params)``.
If two alarms share code, they share it by importing the same helper —
not by being entries in a central registry.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Detection:
    dedupe_key: str              # per-entity identifier, e.g. "task:35981"
    subject: str                 # short line for email subject + dashboard
    body_context: str            # detail text (appended to alarm config body)
    extra_data: dict = field(default_factory=dict)  # structured context
