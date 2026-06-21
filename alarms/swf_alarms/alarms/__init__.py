"""Per-alarm code modules.

Each alarm is a snowflake: one module per alarm_entry_id, named after the
entry_id minus the `alarm_` prefix. The engine imports
``swf_alarms.alarms.<name>`` for alarm ``alarm_<name>`` and calls
``detect(client, params)``, which yields ``Detection`` instances.

Conventions each alarm module follows:

  PARAMS: dict[str, tuple[type, default, description]]
      The parameter surface this alarm reads. Drives the editor's help
      panel. Missing keys use their default; optional keys use None.

  def detect(client, params) -> Iterable[Detection]
      The algorithm. Runs against live data; must not email, must not
      raise on transient failures (log and yield nothing).

Shared helpers (e.g. a common PanDA failure-rate computation) live in
``swf_alarms.lib`` and are imported by the modules that want them —
the snowflake constraint is about ownership, not duplication.
"""
