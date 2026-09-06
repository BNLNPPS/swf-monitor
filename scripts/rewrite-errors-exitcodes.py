"""Add the payload exit code to the errors entries of live snaps recorded
before schema version 4.

The error-state component's entry rows gained the payload's
transformation exit code on 2026-09-06 (schema v4,
docs/SNAPPER_ERRORS.md). Live snaps captured before that carry rows
without it. This one-off rewrite adds the field to each such row in
place, looked up by PanDA job id from the job records, and recomputes
the snap's component hash and state hash by the capture contract
(snapper_ai capture: the sha256 of the canonical JSON of the component
data, and of the components document). Every other field of the row
and the snap stays as recorded; the component document gains an
``augmented`` note naming what was added, when, and how many rows had
no job record left to read. Overflow fold keys name no job and keep
their recorded form.

Idempotent: rows already carrying the field are skipped, and a snap
with nothing to add is not written. Dry-run default; ``--limit N``
bounds an apply to the first N snaps needing the rewrite, for a trial.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-monitor>/scripts/rewrite-errors-exitcodes.py [--apply] [--limit N]
"""

import argparse
import hashlib
import json
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from django.db import connections, transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from monitor_app.panda.constants import PANDA_SCHEMA  # noqa: E402
from monitor_app.snapper_errors import ENTRY_FIELDS, _exit_key  # noqa: E402
from snapper_ai.models import SystemSnap  # noqa: E402

SCOPE = 'epicprod'
COMPONENT = 'errors'
BACKFILL_POLICY = 'backfill-errors-v1'
WIDTH = len(ENTRY_FIELDS)
EXIT_INDEX = ENTRY_FIELDS.index('exitcode')


def _canonical_hash(value):
    """The capture contract's hash: sha256 of the canonical JSON."""
    encoded = json.dumps(
        value, allow_nan=False, ensure_ascii=False,
        separators=(',', ':'), sort_keys=True).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _exit_codes(pandaids):
    """{pandaid: exit key} from the job records, both tables. A job no
    longer in either table is absent from the result."""
    ids = sorted({int(p) for p in pandaids if p})
    out = {}
    if not ids:
        return out
    sql = (
        f'SELECT "pandaid", "transexitcode" '
        f'FROM "{PANDA_SCHEMA}"."jobsarchived4" WHERE "pandaid" = ANY(%s) '
        f'UNION '
        f'SELECT "pandaid", "transexitcode" '
        f'FROM "{PANDA_SCHEMA}"."jobsactive4" WHERE "pandaid" = ANY(%s)'
    )
    with connections['panda'].cursor() as cursor:
        cursor.execute(sql, [ids, ids])
        for pandaid, code in cursor.fetchall():
            out[int(pandaid)] = _exit_key(code)
    return out


def _augment(rows, codes):
    """Add the exit code to every short row in place. Returns
    (rows added to, rows whose job had no record left)."""
    added = unresolved = 0
    for row in rows:
        if len(row) > EXIT_INDEX:
            continue
        while len(row) < EXIT_INDEX:
            row.append('')
        code = codes.get(int(row[0] or 0))
        if code is None:
            unresolved += 1
            code = ''
        row.append(code)
        added += 1
    return added, unresolved


def main():
    parser = argparse.ArgumentParser(
        description='Add the payload exit code to pre-v4 errors entries '
                    'in live epicprod snaps.')
    parser.add_argument('--apply', action='store_true',
                        help='write the rewritten snaps (default: dry run)')
    parser.add_argument('--limit', type=int, default=0,
                        help='with --apply, rewrite at most N snaps')
    args = parser.parse_args()

    snaps = (SystemSnap.objects
             .filter(scope=SCOPE,
                     state__components__errors__data__has_key='entries')
             .exclude(capture_policy=BACKFILL_POLICY)
             .order_by('snap_time'))
    now_iso = timezone.now().isoformat()
    cache = {}
    seen = to_rewrite = rows_added = rows_unresolved = written = 0
    first = last = None
    for snap in snaps.iterator(chunk_size=100):
        seen += 1
        doc = snap.state['components'][COMPONENT]
        data = doc['data']
        rows = data.get('entries') or []
        if not any(len(row) <= EXIT_INDEX for row in rows):
            continue
        interval = data.get('interval') or {}
        key = (interval.get('start'), interval.get('end'))
        if key not in cache:
            cache[key] = _exit_codes(
                row[0] for row in rows if len(row) <= EXIT_INDEX)
        added, unresolved = _augment(rows, cache[key])
        to_rewrite += 1
        rows_added += added
        rows_unresolved += unresolved
        first = first or snap.snap_time
        last = snap.snap_time
        if not args.apply or (args.limit and written >= args.limit):
            continue
        doc['augmented'] = {
            'exitcode': now_iso, 'rows': added, 'unresolved': unresolved}
        hashes = dict(snap.component_hashes or {})
        hashes[COMPONENT] = _canonical_hash(data)
        state_hash = _canonical_hash(snap.state['components'])
        with transaction.atomic():
            SystemSnap.objects.filter(pk=snap.pk).update(
                state=snap.state, component_hashes=hashes,
                state_hash=state_hash)
        written += 1

    print(f'live snaps carrying entries: {seen}; needing the rewrite: '
          f'{to_rewrite} ({first} to {last}); distinct intervals looked '
          f'up: {len(cache)}')
    print(f'rows to add the code to: {rows_added}; rows whose job has no '
          f'record left (code left empty): {rows_unresolved}')
    if not args.apply:
        print('\ndry run — nothing written; --apply rewrites the snaps')
        return 0
    print(f'\napplied: rewrote {written} snaps'
          + (f' (limit {args.limit})' if args.limit else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
