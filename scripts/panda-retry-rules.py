"""Manage the PanDA retry-module rules for the epic instance.

The retry module (panda-server retryModule.py) applies error-keyed
rules on top of ordinary per-attempt retries: RETRYERRORS rows match a
failed job's error source, code, and diagnostic pattern and invoke a
RETRYACTIONS implementation (no_retry, limit_retry, ...). A rule
enforces only when BOTH switches are set: retryerrors.active = 'Y'
and retryactions.active = 'Y' (db_proxy_mods/misc_standalone_module.py,
getRetrialRules); otherwise the module logs what it would have done.
JEDI caches the rule set, so a change takes effect within about an
hour.

This script is the write surface for the rule-level switch
(retryerrors.active) and reports both tables. The action-level switch
(retryactions.active) disables an action for every rule that uses it
and is deliberately not managed here. The epic rule set and its
rationale: swf-epicprod docs/PANDA_ANCILLARY_AUDIT.md. The System
page's PanDA Configuration section shows the same tables live.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-monitor>/scripts/panda-retry-rules.py            # list
    python <swf-monitor>/scripts/panda-retry-rules.py --activate 1 --apply
    python <swf-monitor>/scripts/panda-retry-rules.py --deactivate 1 --apply

Dry-run without --apply: shows the switch changes it would make.
"""

import argparse
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from django.db import connections, transaction  # noqa: E402


def list_rules():
    from monitor_app.system_status import (
        panda_retry_action_set,
        panda_retry_rule_set,
    )
    print('Rules (RETRYERRORS):')
    for r in panda_retry_rule_set():
        print(f"  id {r['id']}: {r['source']} {r['code']} "
              f"diag {r['diag']!r} {r['parameters']} "
              f"-> {r['action']}  [{r['mode']}]")
    print('Actions (RETRYACTIONS):')
    for a in panda_retry_action_set():
        state = 'active' if a['active'] else 'INACTIVE'
        print(f"  id {a['id']}: {a['action']} [{state}] {a['description']}")


def set_rule_active(rule_ids, value, apply_changes):
    with connections['panda'].cursor() as cursor:
        placeholders = ', '.join(['%s'] * len(rule_ids))
        cursor.execute(
            f"SELECT retryerror_id, errorsource, errorcode, active"
            f" FROM retryerrors WHERE retryerror_id IN ({placeholders})",
            rule_ids)
        rows = {row[0]: row for row in cursor.fetchall()}
    missing = [i for i in rule_ids if i not in rows]
    if missing:
        print(f'ERROR: no such rule id(s): {missing}', file=sys.stderr)
        return 1
    to_change = [i for i in rule_ids if rows[i][3] != value]
    for rule_id in rule_ids:
        _, source, code, current = rows[rule_id]
        note = ('unchanged' if rows[rule_id][3] == value
                else f"{current} -> {value}")
        print(f'  rule {rule_id} ({source} {code}): {note}')
    if not to_change:
        print('nothing to change')
        return 0
    if not apply_changes:
        print('dry run — nothing written; --apply writes the switches')
        return 0
    with transaction.atomic(using='panda'):
        with connections['panda'].cursor() as cursor:
            placeholders = ', '.join(['%s'] * len(to_change))
            cursor.execute(
                f"UPDATE retryerrors SET active = %s"
                f" WHERE retryerror_id IN ({placeholders})",
                [value] + to_change)
            if cursor.rowcount != len(to_change):
                raise RuntimeError(
                    f'expected {len(to_change)} rows updated, '
                    f'got {cursor.rowcount}')
    print(f'applied: {len(to_change)} rule(s) set active={value}; '
          f'JEDI picks this up within ~1 hour (rule cache)')
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='List or switch PanDA retry-module rules '
                    '(rule-level active flag).')
    parser.add_argument('--activate', type=int, nargs='+', metavar='ID',
                        help='set active=Y on these RETRYERRORS ids')
    parser.add_argument('--deactivate', type=int, nargs='+', metavar='ID',
                        help='set active=N on these RETRYERRORS ids')
    parser.add_argument('--apply', action='store_true',
                        help='write the switches (dry run without)')
    args = parser.parse_args()

    if args.activate and args.deactivate:
        overlap = set(args.activate) & set(args.deactivate)
        if overlap:
            print(f'ERROR: ids in both --activate and --deactivate: '
                  f'{sorted(overlap)}', file=sys.stderr)
            return 1
    status = 0
    if args.activate:
        status = set_rule_active(args.activate, 'Y', args.apply) or status
    if args.deactivate:
        status = set_rule_active(args.deactivate, 'N', args.apply) or status
    if not args.activate and not args.deactivate:
        list_rules()
        return 0
    print()
    list_rules()
    return status


if __name__ == '__main__':
    sys.exit(main())
