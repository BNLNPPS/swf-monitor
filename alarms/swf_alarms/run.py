"""swf-alarms engine entry point.

Per-tick behavior (active/clear semantics):

  1. Load kind='alarm' entries (not archived) from the DB. `data.enabled`
     is NOT consulted here — the algorithm runs for every non-archived
     alarm every tick. `enabled` gates only email delivery (see step 3).
     "Stop running this alarm" is expressed as archived=True.
  2. For each alarm config:
     a. Fetch existing active events for this alarm (one per entity still
        firing) — indexed by data.dedupe_key.
     b. Call the alarm's ``detect(client, params)`` and collect the
        dedupe_keys it yields this tick.
     c. For each detection:
          - If already active for same dedupe_key: touch last_seen. If
            the alarm's emails are on AND the event has never been
            notified (created while emails-off) OR the per-alarm
            renotification window has elapsed, add to the RENOTIFY
            bundle.
          - Else: create a new event entry (fire_time=now, clear_time=null),
            compose and store the single-detection body, add to the NEW
            bundle. Event rows are created unconditionally of `enabled`
            so the dashboard stays truthful.
     d. For each previously-active event NOT in the current detection set:
        set data.clear_time = now (auto-clear — unconditional of `enabled`).
  3. If the alarm's emails are on AND (NEW or RENOTIFY) is non-empty:
     compose ONE bundled email listing every detection in both sections
     and ship it via SES. On success, stamp `last_notified = now` on
     every included event. ``notifications_sent`` counts this as one,
     regardless of bundle size. Closes out the `engine_run` entry with
     aggregate counters + per-alarm detail (including `bundle_new`,
     `bundle_renotify`, `bundle_sent`).

No cooldown flag: dedup is *state-based* — one active event per (alarm,
entity). Re-firings only happen after a cleared→fire transition, plus
the optional renotification window re-emails a still-active event (as
part of the next bundle).
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
import time
import traceback

from . import config as config_mod
from . import db
from .common import Detection
from .fetch import Client, FetchError
from .notify import Alarm, send_email_ses


def _load_alarm_module(alarm_entry_id: str):
    """Return the per-alarm module for ``alarm_<name>``.

    Snowflake dispatch: one Python file per alarm, named
    ``swf_alarms/alarms/<name>.py``, exposing ``detect(client, params)``.
    """
    name = alarm_entry_id[len('alarm_'):] if alarm_entry_id.startswith('alarm_') else alarm_entry_id
    return importlib.import_module(f"swf_alarms.alarms.{name}")


log = logging.getLogger("swf_alarms")


def _configure_logging(log_path: str | None, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_path:
        handlers.append(logging.FileHandler(log_path))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers, force=True,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="swf-alarms-run")
    ap.add_argument("--config", required=True, help="path to engine TOML")
    ap.add_argument("--dry-run", action="store_true",
                    help="detect and persist, but suppress notifications")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    cfg = config_mod.load(args.config)
    _configure_logging(cfg.engine.log_path, args.verbose)

    log.info("run starting  config=%s  dry_run=%s", args.config, args.dry_run)
    conn = db.connect(cfg.db_dsn)
    run_uuid = db.start_engine_run(conn)

    client = Client(cfg.engine.service_base_url,
                    timeout=cfg.engine.request_timeout)

    # Load ALL non-archived alarm configs. Per-alarm `data.enabled` gates
    # ONLY the email side — detection, event creation, active/clear,
    # dashboard visibility keep working regardless. "Disabled" means
    # "silent": alarm reporting still happens, emails do not.
    alarm_configs = db.list_alarm_configs(conn, enabled_only=False)
    log.info("loaded %d alarm config(s) — algorithms always run; "
             "per-alarm `enabled` controls email only", len(alarm_configs))

    alarms_run = 0
    alarms_seen = 0
    notifications_sent = 0
    errors = 0
    error_traces: list[str] = []
    per_alarm: dict[str, dict] = {}

    for alarm in alarm_configs:
        data = alarm.get("data") or {}
        alarm_entry_id = data.get("entry_id", "")
        if not alarm_entry_id:
            log.error("alarm %s missing data.entry_id — skipping", alarm["id"])
            errors += 1
            continue
        try:
            alarm_mod = _load_alarm_module(alarm_entry_id)
            fn = alarm_mod.detect
        except (ImportError, AttributeError) as e:
            msg = f"no alarm module for {alarm_entry_id!r}: {e}"
            log.error("%s — skipping", msg)
            errors += 1
            error_traces.append(f"[{alarm_entry_id}] {msg}")
            per_alarm[alarm_entry_id] = {
                "enabled": True, "alarms_seen": 0,
                "errors": 1, "error_message": msg,
            }
            continue

        alarms_run += 1
        params = dict(data.get("params") or {})
        raw_recipients = data.get("recipients") or []
        # Accept either string (user-typed, stored verbatim) or list (legacy).
        if isinstance(raw_recipients, str):
            raw_recipients = db._split_tokens(raw_recipients)
        else:
            raw_recipients = list(raw_recipients)
        # Resolve @<team> tokens to member emails via DB lookup.
        recipients, unresolved_teams = db.resolve_recipients(conn, raw_recipients)
        if unresolved_teams:
            log.warning("alarm %s: unresolved team(s) %s — skipped",
                        alarm_entry_id, unresolved_teams)
        # Per-alarm renotification window (hours). 0 / missing = no
        # re-notification — a still-firing entity stays quiet until clear.
        renotification_window_hours = float(
            data.get("renotification_window_hours") or 0)

        alarm_seen = 0
        alarm_err = 0
        alarm_err_msg = ""
        detected_keys: set[str] = set()

        # Per-alarm email gate. When False, event rows still fire and
        # active/clear still tick — we just don't ship mail.
        email_enabled = bool(data.get("enabled", True))
        send_mail = email_enabled and not args.dry_run

        # Bundle buckets: at most ONE email per alarm per tick, listing
        # every detection that would otherwise have triggered a send
        # this tick. Entries are (event_uuid, Detection).
        new_bundle: list[tuple[str, Detection]] = []
        renotify_bundle: list[tuple[str, Detection]] = []

        # Existing active events for this alarm — map dedupe_key → row
        try:
            active_rows = db.active_events_for_alarm(conn, alarm_entry_id)
        except Exception as e:  # noqa: BLE001
            log.error("active_events_for_alarm(%s) failed: %s",
                      alarm_entry_id, e)
            active_rows = []
        active_by_key = {
            (r.get("data") or {}).get("dedupe_key"): r for r in active_rows
        }

        try:
            for det in fn(client, params):  # type: Detection
                alarm_seen += 1
                alarms_seen += 1
                detected_keys.add(det.dedupe_key)
                existing = active_by_key.get(det.dedupe_key)

                if existing is None:
                    # New entity crosses threshold → create the event row
                    # unconditionally (dashboard must reflect truth even
                    # when emails are off). The event body is the fully
                    # composed single-detection body — same as before —
                    # so the event-detail page reads naturally. The
                    # bundle for this tick adds it to `new_bundle`.
                    full_body = _compose_body(
                        alarm.get("content") or "", det.body_context)
                    event_uuid = db.create_event(
                        conn,
                        alarm_entry_id=alarm_entry_id,
                        dedupe_key=det.dedupe_key,
                        subject=det.subject,
                        body=full_body,
                        recipients=recipients,
                        extra_data=det.extra_data,
                        alarm_config_uuid=alarm["id"],
                    )
                    new_bundle.append((event_uuid, det))
                    continue

                # Same entity already firing — always bump last_seen.
                db.touch_event_last_seen(conn, existing["id"])

                # Renotification candidacy (bundled, not sent per-detection):
                # computed independently of send_mail so that the
                # per-run report can show what WOULD have been emailed
                # even when this alarm's emails are off. We gate the
                # actual SES call later on send_mail.
                #   - Event never notified (last_notified missing/0 —
                #     e.g. created while emails were off) → always bundle.
                #   - Otherwise: renotification window must be set and
                #     elapsed.
                last_notified = float(
                    (existing.get("data") or {}).get("last_notified") or 0)
                if last_notified == 0:
                    renotify_bundle.append((existing["id"], det))
                elif renotification_window_hours > 0 and (
                        time.time() - last_notified
                        >= renotification_window_hours * 3600):
                    renotify_bundle.append((existing["id"], det))
        except FetchError as e:
            log.error("alarm %s fetch failed: %s", alarm_entry_id, e)
            errors += 1
            alarm_err = 1
            alarm_err_msg = f"fetch: {e}"
            error_traces.append(f"[{alarm_entry_id}] fetch: {e}")
        except Exception:  # noqa: BLE001
            tb = traceback.format_exc()
            log.error("alarm %s raised:\n%s", alarm_entry_id, tb)
            errors += 1
            alarm_err = 1
            alarm_err_msg = tb
            error_traces.append(f"[{alarm_entry_id}]\n{tb}")

        # Auto-clear events whose dedupe_key wasn't seen this tick. Only
        # do so if the alarm ran without error — otherwise a transient
        # fetch failure would clear everything.
        if alarm_err == 0:
            for key, ev in active_by_key.items():
                if key not in detected_keys:
                    try:
                        db.clear_event(conn, ev["id"])
                    except Exception as e:  # noqa: BLE001
                        log.error("clear_event failed for %s: %s", ev["id"], e)

        # One email per alarm per tick. Compose the bundle whenever
        # there's anything to bundle — store its identifiers on the
        # engine_run row so the dashboard can show a per-run report
        # regardless of whether email went out. Send SES only when
        # this alarm's emails are on.
        bundle_sent = False
        bundle_new = len(new_bundle)
        bundle_renotify = len(renotify_bundle)
        bundle_subject = ""
        if new_bundle or renotify_bundle:
            bundle_subject, body = _compose_bundle(
                alarm_entry_id=alarm_entry_id,
                alarm_description=alarm.get("content") or "",
                new_bundle=new_bundle,
                renotify_bundle=renotify_bundle,
            )
            if send_mail:
                ok = send_email_ses(
                    Alarm(
                        alarm_name=alarm_entry_id,
                        dedupe_key=f"bundle:{int(time.time())}",
                        subject=bundle_subject,
                        body=body,
                        recipients=list(recipients),
                        data={
                            "bundle": True,
                            "new_count": bundle_new,
                            "renotify_count": bundle_renotify,
                        },
                    ),
                    region=cfg.email.region,
                    from_addr=cfg.email.from_addr,
                )
                if ok:
                    for event_uuid, _ in new_bundle:
                        db.mark_event_notified(conn, event_uuid)
                    for event_uuid, _ in renotify_bundle:
                        db.mark_event_notified(conn, event_uuid)
                    notifications_sent += 1  # exactly one email, regardless of count
                    bundle_sent = True

        per_alarm[alarm_entry_id] = {
            "enabled": email_enabled,
            "alarms_seen": alarm_seen,
            "errors": alarm_err,
            "error_message": alarm_err_msg,
            "bundle_new": bundle_new,
            "bundle_renotify": bundle_renotify,
            "bundle_sent": bundle_sent,
            "bundle_subject": bundle_subject,
            "bundle_new_event_ids": [u for u, _ in new_bundle],
            "bundle_renotify_event_ids": [u for u, _ in renotify_bundle],
            "params": params,
            "recipients": recipients,
        }

    db.finish_engine_run(
        conn, run_uuid,
        alarms_run=alarms_run,
        alarms_seen=alarms_seen,
        notifications_sent=notifications_sent,
        errors=errors,
        error_details="\n\n".join(error_traces),
        per_alarm=per_alarm,
    )
    log.info("run done  alarms_run=%d  alarms_seen=%d  sent=%d  errors=%d",
             alarms_run, alarms_seen, notifications_sent, errors)
    return 0 if errors == 0 else 1


def _compose_body(alarm_description: str, detail: str) -> str:
    """Single-detection body — used when persisting the event row so the
    event-detail page reads as a standalone record. The per-tick email
    is built by `_compose_bundle` and may contain many such details."""
    if alarm_description and alarm_description.strip():
        return f"{alarm_description.rstrip()}\n\n---\n\n{detail}"
    return detail


def _compose_bundle(*, alarm_entry_id: str, alarm_description: str,
                    new_bundle: list, renotify_bundle: list) -> tuple[str, str]:
    """Build ONE email covering every detection that warrants mail this
    tick for this alarm. Returns (subject, body).

    Subject: "[{alarm_entry_id}] {N} detection(s): {X} new, {Y} continuing"
    Body: alarm description, then a "New" section, then a "Continuing"
    section. Each section lists its detections with subject + indented
    body_context.
    """
    n_new = len(new_bundle)
    n_ren = len(renotify_bundle)
    n_total = n_new + n_ren
    parts = [f"{n_total} detection(s)"]
    if n_new:
        parts.append(f"{n_new} new")
    if n_ren:
        parts.append(f"{n_ren} continuing")
    subject = f"[{alarm_entry_id}] {', '.join(parts)}"

    lines: list[str] = []
    desc = (alarm_description or "").rstrip()
    if desc:
        lines.append(desc)
        lines.append("")
        lines.append("---")
        lines.append("")

    def _append_section(header: str, items: list) -> None:
        if not items:
            return
        lines.append(header)
        lines.append("")
        for i, (_uuid, det) in enumerate(items, 1):
            lines.append(f"  [{i}] {det.subject}")
            if det.body_context:
                for body_line in det.body_context.splitlines():
                    lines.append(f"      {body_line}")
            lines.append("")
        lines.append("")

    _append_section(f"NEW ({n_new}):", new_bundle)
    _append_section(f"CONTINUING — renotification ({n_ren}):", renotify_bundle)

    return subject, "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    sys.exit(main())
