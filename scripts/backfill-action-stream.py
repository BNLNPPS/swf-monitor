#!/usr/bin/env python
"""One-off backfill of early epicprod action-stream records.

Records written before the sublevel/live axes (2026-07-05) carry no
sublevel/live_default and are therefore invisible to the live view and its
channels; records written before the reason plumbing carry non-ok outcomes
with no failure reason. This stamps both, from recoverable sources only:

- axes: sublevel/live_default from the ACTION_DEFAULTS catalog by action id
  (an action absent from the catalog gets low/False, the conservative
  default).
- reason, payload_log_fetch errors: the cache .error marker's last_error,
  written by the same failed fetch ($SWF_TMP_DIR/panda-logs/<task>/<job>/).
- reason, association_sweep timeout: deterministic from the outcome and the
  record's own measured duration.
- reason, questionnaire_import skipped: the documented skip cause —
  SysConfig questionnaire_csv_url unset.

Anything else non-ok with no recoverable cause is reported and left alone —
a backfill must not fabricate. Backfilled records are marked
extra_data['backfilled'] with what was stamped. Dry run by default; --apply
writes.
"""
import argparse
import json
import os


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="write changes (default: dry-run report)")
    args = parser.parse_args()

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "swf_monitor_project.settings")
    import django
    django.setup()
    from monitor_app.epicprod_logging import (ACTION_DEFAULTS,
                                              EPICPROD_APP_NAME,
                                              SUBLEVEL_VALUES)
    from monitor_app.models import AppLog

    swf_tmp = os.environ.get("SWF_TMP_DIR", "/data/swf-tmp")
    stamped_axes = stamped_reason = unrecovered = 0

    rows = (AppLog.objects.filter(app_name=EPICPROD_APP_NAME)
            .exclude(extra_data__isnull=True).order_by('id'))
    for row in rows.iterator():
        extra = row.extra_data if isinstance(row.extra_data, dict) else {}
        action = str(extra.get('action') or '')
        if not action:
            continue
        changed = []

        if extra.get('sublevel') not in SUBLEVEL_VALUES:
            decl = ACTION_DEFAULTS.get(action) or {}
            extra['sublevel'] = decl.get('sublevel', 'low')
            extra['live_default'] = bool(decl.get('live', False))
            changed.append('axes')

        outcome = str(extra.get('outcome') or '')
        if outcome not in ('', 'ok') and not extra.get('reason'):
            reason = ''
            if action == 'payload_log_fetch':
                marker = os.path.join(swf_tmp, 'panda-logs',
                                      str(extra.get('jeditaskid') or ''),
                                      str(extra.get('subject_key') or ''),
                                      '.error')
                try:
                    with open(marker) as f:
                        reason = str(json.load(f).get('last_error') or '')
                except (OSError, ValueError):
                    reason = ''
            elif action == 'association_sweep' and outcome == 'timeout':
                dur = extra.get('duration_ms')
                reason = (f"timed out after {int(dur) // 1000}s"
                          if dur else "timed out")
            elif action == 'questionnaire_import' and outcome == 'skipped':
                reason = 'questionnaire_csv_url unset'
            if reason:
                extra['reason'] = reason[:300]
                row.message = f"{row.message} — {reason[:300]}"
                changed.append('reason')
            else:
                unrecovered += 1
                print(f"UNRECOVERED: #{row.id} {row.timestamp} {action} "
                      f"{outcome} — no recoverable cause, left alone")

        if changed:
            stamped_axes += 'axes' in changed
            stamped_reason += 'reason' in changed
            extra['backfilled'] = '+'.join(changed)
            print(f"{'APPLY' if args.apply else 'DRY'}: #{row.id} "
                  f"{row.timestamp} {action} <- {extra['backfilled']}"
                  + (f" ({extra.get('reason', '')})" if 'reason' in changed else ''))
            if args.apply:
                row.extra_data = extra
                row.save(update_fields=['extra_data', 'message'])

    print(f"\nsummary: axes={stamped_axes} reason={stamped_reason} "
          f"unrecovered={unrecovered} applied={bool(args.apply)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
