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
(retryerrors.active), adds actions and rules, and reports both tables.
The action-level switch (retryactions.active) disables an action for
every rule that uses it and is deliberately not managed here; an added
action is active. The epic rule set and its rationale: swf-epicprod
docs/PANDA_ANCILLARY_AUDIT.md. The System page's PanDA Configuration
section shows the same tables live.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-monitor>/scripts/panda-retry-rules.py            # list
    python <swf-monitor>/scripts/panda-retry-rules.py --activate 1 --apply
    python <swf-monitor>/scripts/panda-retry-rules.py --deactivate 1 --apply
    python <swf-monitor>/scripts/panda-retry-rules.py \\
        --add-action increase_memory --description '...' --apply
    python <swf-monitor>/scripts/panda-retry-rules.py \\
        --add-rule pilotErrorCode 1212 increase_memory \\
        --diag '.*out of memory.*' --description '...' --apply

Dry-run without --apply: shows the changes it would make.
"""

import argparse
import os
import re
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from django.db import connections, transaction  # noqa: E402

# The job error fields the retry module reads (retryModule.py,
# get_job_error_details) and the actions it implements.
ERROR_SOURCES = ('pilotErrorCode', 'exeErrorCode', 'supErrorCode',
                 'ddmErrorCode', 'brokerageErrorCode',
                 'jobDispatcherErrorCode', 'taskBufferErrorCode')
ACTIONS = ('no_retry', 'limit_retry', 'increase_memory',
           'increase_memory_xtimes', 'increase_cputime',
           'reduce_input_per_job')


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


def _next_id(cursor, table, column):
    cursor.execute(f"SELECT COALESCE(MAX({column}), 0) + 1 FROM {table}")
    return cursor.fetchone()[0]


def add_action(name, description, apply_changes):
    if name not in ACTIONS:
        print(f'ERROR: {name!r} is not a retry-module action {ACTIONS}',
              file=sys.stderr)
        return 1
    with connections['panda'].cursor() as cursor:
        cursor.execute("SELECT retryaction_id FROM retryactions"
                       " WHERE retry_action = %s", [name])
        row = cursor.fetchone()
    if row:
        print(f'action {name} exists (id {row[0]}); nothing to add')
        return 0
    print(f'  add action {name} [active]: {description}')
    if not apply_changes:
        print('dry run — nothing written; --apply adds it')
        return 0
    with transaction.atomic(using='panda'):
        with connections['panda'].cursor() as cursor:
            action_id = _next_id(cursor, 'retryactions', 'retryaction_id')
            cursor.execute(
                "INSERT INTO retryactions"
                " (retryaction_id, retry_action, active, retry_description)"
                " VALUES (%s, %s, 'Y', %s)",
                [action_id, name, description])
    print(f'applied: action {name} added as id {action_id}')
    return 0


def add_rule(source, code, action, diag, parameters, description, active,
             apply_changes):
    if source not in ERROR_SOURCES:
        print(f'ERROR: {source!r} is not a job error source {ERROR_SOURCES}',
              file=sys.stderr)
        return 1
    if diag:
        try:
            re.compile(diag)
        except re.error as exc:
            print(f'ERROR: --diag is not a regular expression: {exc}',
                  file=sys.stderr)
            return 1
    with connections['panda'].cursor() as cursor:
        cursor.execute("SELECT retryaction_id FROM retryactions"
                       " WHERE retry_action = %s", [action])
        row = cursor.fetchone()
        if not row:
            print(f'ERROR: no action {action!r} in RETRYACTIONS; '
                  f'add it with --add-action first', file=sys.stderr)
            return 1
        action_id = row[0]
        cursor.execute(
            "SELECT retryerror_id FROM retryerrors"
            " WHERE errorsource = %s AND errorcode = %s AND retryaction = %s"
            " AND COALESCE(errordiag, '') = %s",
            [source, code, action_id, diag or ''])
        existing = cursor.fetchone()
    if existing:
        print(f'rule exists (id {existing[0]}); nothing to add')
        return 0
    state = 'Y' if active else 'N'
    print(f'  add rule {source} {code} diag {diag!r} {parameters or ""} '
          f'-> {action} [active={state}]: {description}')
    if not apply_changes:
        print('dry run — nothing written; --apply adds it')
        return 0
    with transaction.atomic(using='panda'):
        with connections['panda'].cursor() as cursor:
            rule_id = _next_id(cursor, 'retryerrors', 'retryerror_id')
            cursor.execute(
                "INSERT INTO retryerrors"
                " (retryerror_id, errorsource, errorcode, active, retryaction,"
                "  errordiag, parameters, description)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [rule_id, source, code, state, action_id, diag or None,
                 parameters or None, description])
    print(f'applied: rule added as id {rule_id}; '
          f'JEDI picks this up within ~1 hour (rule cache)')
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='List, switch or add PanDA retry-module rules.')
    parser.add_argument('--activate', type=int, nargs='+', metavar='ID',
                        help='set active=Y on these RETRYERRORS ids')
    parser.add_argument('--deactivate', type=int, nargs='+', metavar='ID',
                        help='set active=N on these RETRYERRORS ids')
    parser.add_argument('--add-action', metavar='NAME',
                        help='add a RETRYACTIONS row (active) for this action')
    parser.add_argument('--add-rule', nargs=3,
                        metavar=('SOURCE', 'CODE', 'ACTION'),
                        help='add a RETRYERRORS rule, e.g. pilotErrorCode '
                             '1212 increase_memory')
    parser.add_argument('--diag', help='rule diagnostic regular expression')
    parser.add_argument('--parameters', help="rule parameters, e.g. "
                                             "'maxAttempt=2'")
    parser.add_argument('--description',
                        help='what the added action or rule is for (required)')
    parser.add_argument('--passive', action='store_true',
                        help='add the rule with active=N (logs only)')
    parser.add_argument('--apply', action='store_true',
                        help='write the changes (dry run without)')
    args = parser.parse_args()

    if args.add_action or args.add_rule:
        if args.add_action and args.add_rule:
            print('ERROR: add one action or one rule per run', file=sys.stderr)
            return 1
        if not args.description:
            print('ERROR: --description is required', file=sys.stderr)
            return 1
        if args.add_action:
            status = add_action(args.add_action, args.description, args.apply)
        else:
            source, code, action = args.add_rule
            try:
                code = int(code)
            except ValueError:
                print(f'ERROR: CODE must be an integer, got {code!r}',
                      file=sys.stderr)
                return 1
            status = add_rule(source, code, action, args.diag,
                              args.parameters, args.description,
                              not args.passive, args.apply)
        print()
        list_rules()
        return status

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
