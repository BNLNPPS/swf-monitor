#!/usr/bin/env python3
"""whats-new-proposals-retire.py — retire the pending What's new AI
proposals (swf-monitor docs/WHATS_NEW.md, Items added by LLM sessions).
What's new items are added as part of the task that ships a change, not
proposed (Torre Wenaus, 2026-11-12), so the proposals the retired
``whats_new`` category left pending are settled here: each one named in
``--add`` becomes its item, through ``help.whats_new.item_create`` with
the proposal as its origin, and is marked executed; each one named in
``--withdraw`` (a duplicate, or an item not wanted) is marked withdrawn.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ~/.env
    /data/wenauseic/github/swf-testbed/.venv/bin/python \\
        ../scripts/whats-new-proposals-retire.py --add 3974 3975 \\
        --withdraw 3978 --by wenaus --decided-by swf-1 [--apply]

Dry run by default: what would be added and withdrawn is printed.
"""
import argparse
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from ai.models import Proposal  # noqa: E402
from ai.services import _mark_decided  # noqa: E402
from help import whats_new  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--add', type=int, nargs='*', default=[],
                    help='proposal ids whose items are added')
    ap.add_argument('--withdraw', type=int, nargs='*', default=[],
                    help='proposal ids withdrawn without an item')
    ap.add_argument('--by', required=True,
                    help='the person the added items record as who added them')
    ap.add_argument('--decided-by', required=True,
                    help='recorded on each settled proposal')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    ids = args.add + args.withdraw
    rows = {r.pk: r for r in Proposal.objects.filter(
        pk__in=ids, action='whats_new', status='proposed')}
    missing = sorted(set(ids) - set(rows))
    if missing:
        sys.exit(f'not pending What\'s new proposals: {missing}')
    now = timezone.now()
    failed = 0
    for pk in args.add:
        row = rows[pk]
        fields = {k: (row.payload or {}).get(k) for k in whats_new.FIELDS}
        print(f'add {row.ref}: {fields["title"]}')
        if not args.apply:
            continue
        origin = {'kind': 'ai_proposal', 'ref': row.ref,
                  'proposer': row.proposer, 'batch_id': row.batch_id,
                  'proposed_at': row.created_at.isoformat()}
        try:
            with transaction.atomic():
                entry, log_id = whats_new.item_create(
                    fields, changed_by=args.by, origin=origin)
                _mark_decided(row, 'executed', args.decided_by, now,
                              quality='', log_id=log_id)
        except whats_new.WhatsNewError as e:
            failed += 1
            print(f'  refused: {e.detail}', file=sys.stderr)
            continue
        print(f'  added /help/whats-new/{entry.name}/')
    for pk in args.withdraw:
        row = rows[pk]
        print(f'withdraw {row.ref}: {(row.payload or {}).get("title")}')
        if args.apply:
            _mark_decided(row, 'withdrawn', args.decided_by, now)
    if not args.apply:
        print('dry run: nothing written (--apply to write)')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
