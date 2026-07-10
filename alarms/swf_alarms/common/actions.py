"""Helpers over the epicprod action stream for alarm detectors.

The action stream is the structured record of epicprod actions in
``swf_applog`` (``app_name='epicprod'``, structured fields in ``extra_data``)
— see swf-monitor docs/EPICPROD_ACTION_STREAM.md. Detectors reach it through
``client.db_conn``, the engine's psycopg connection attached in run.py.
"""
from __future__ import annotations


def latest_action(conn, action: str, *, outcome: str | None = None):
    """Newest action record as a dict (timestamp, extra_data), or None."""
    sql = (
        "SELECT timestamp, extra_data FROM swf_applog "
        "WHERE app_name = 'epicprod' AND extra_data->>'action' = %s"
    )
    params = [action]
    if outcome is not None:
        sql += " AND extra_data->>'outcome' = %s"
        params.append(outcome)
    sql += " ORDER BY id DESC LIMIT 1"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def action_count(conn, action: str, *, minutes: int) -> int:
    """Count of action records in the trailing window."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM swf_applog "
            "WHERE app_name = 'epicprod' AND extra_data->>'action' = %s "
            "AND timestamp >= now() - make_interval(mins => %s)",
            [action, minutes])
        row = cur.fetchone()
        return int(row["n"]) if row else 0
