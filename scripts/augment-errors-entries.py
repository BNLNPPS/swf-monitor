"""Add missing trailing entry fields to the errors entries of live snaps
recorded before the current schema version.

The error-state component's entry rows grow at the end: the terminal
status at schema version 3, the payload's transformation exit code at
version 4, the core-seconds held at version 5 (docs/SNAPPER_ERRORS.md).
Live snaps captured before a field joined carry rows without it. This
augmentation adds every trailing field a row lacks, in place, looked up
by PanDA job id from the job records with the publisher's own
definitions, and recomputes the snap's component hash and state hash
by the capture contract (snapper_ai capture: the sha256 of the
canonical JSON of the component data, and of the components document).
Every other field of the row and the snap stays as recorded; the
component document carries an ``augmented`` note per field naming when
it was added, how many rows received it, and how many had no job record
left to read (those take the field's empty value). Overflow fold keys
name no job and keep their recorded form.

Idempotent: rows already carrying every field are skipped, and a snap
with nothing to add is not written. Dry-run default; ``--limit N``
bounds an apply to the first N snaps needing the augmentation, for a
trial.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-monitor>/scripts/augment-errors-entries.py [--apply] [--limit N]
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
from monitor_app.snapper_errors import (  # noqa: E402
    ENTRY_FIELDS,
    HELD_SQL,
    _exit_key,
)
from snapper_ai.models import SystemSnap  # noqa: E402

SCOPE = 'epicprod'
COMPONENT = 'errors'
BACKFILL_POLICY = 'backfill-errors-v1'
WIDTH = len(ENTRY_FIELDS)
# The value a field takes when the job has no record left to read.
EMPTY = {'status': '', 'exitcode': '', 'held': 0}


def _canonical_hash(value):
    """The capture contract's hash: sha256 of the canonical JSON."""
    encoded = json.dumps(
        value, allow_nan=False, ensure_ascii=False,
        separators=(',', ':'), sort_keys=True).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _job_fields(pandaids):
    """{pandaid: {field: value}} for the fields the job records supply
    — status, exitcode and held — over both tables. A job no longer in
    either table is absent from the result."""
    ids = sorted({int(p) for p in pandaids if p})
    out = {}
    if not ids:
        return out
    columns = (
        '"pandaid", "jobstatus", "transexitcode", "starttime", '
        '"endtime" AS "ended", "actualcorecount", "corecount"'
    )
    sql = (
        f'SELECT "pandaid", "jobstatus", "transexitcode", {HELD_SQL} '
        f'FROM (SELECT {columns} FROM "{PANDA_SCHEMA}"."jobsarchived4" '
        f'WHERE "pandaid" = ANY(%s) '
        f'UNION SELECT {columns} FROM "{PANDA_SCHEMA}"."jobsactive4" '
        f'WHERE "pandaid" = ANY(%s)) jobs'
    )
    with connections['panda'].cursor() as cursor:
        cursor.execute(sql, [ids, ids])
        for pandaid, status, exitcode, held in cursor.fetchall():
            out[int(pandaid)] = {
                'status': str(status or ''),
                'exitcode': _exit_key(exitcode),
                'held': int(held or 0),
            }
    return out


def _augment(rows, jobs):
    """Add every missing trailing field to each short row in place.
    Returns {field: (rows added to, rows whose job had no record)}."""
    stats = {}
    for row in rows:
        if len(row) >= WIDTH:
            continue
        job = jobs.get(int(row[0] or 0))
        for index in range(len(row), WIDTH):
            field = ENTRY_FIELDS[index]
            added, unresolved = stats.get(field) or (0, 0)
            if job is None:
                row.append(EMPTY.get(field, ''))
                unresolved += 1
            else:
                row.append(job.get(field, EMPTY.get(field, '')))
            stats[field] = (added + 1, unresolved)
    return stats


def _note(doc, stats, now_iso):
    """The augmented note: one entry per field added. An earlier note
    in the first form (a timestamp under 'exitcode' with 'rows' and
    'unresolved' beside it) is carried into the per-field form."""
    note = doc.get('augmented') or {}
    if isinstance(note.get('exitcode'), str):
        note = {'exitcode': {'at': note['exitcode'],
                             'rows': note.get('rows', 0),
                             'unresolved': note.get('unresolved', 0)}}
    for field, (added, unresolved) in stats.items():
        note[field] = {'at': now_iso, 'rows': added,
                       'unresolved': unresolved}
    doc['augmented'] = note


def main():
    parser = argparse.ArgumentParser(
        description='Add missing trailing entry fields to pre-current '
                    'errors entries in live epicprod snaps.')
    parser.add_argument('--apply', action='store_true',
                        help='write the augmented snaps (default: dry run)')
    parser.add_argument('--limit', type=int, default=0,
                        help='with --apply, augment at most N snaps')
    args = parser.parse_args()

    snaps = (SystemSnap.objects
             .filter(scope=SCOPE,
                     state__components__errors__data__has_key='entries')
             .exclude(capture_policy=BACKFILL_POLICY)
             .order_by('snap_time'))
    now_iso = timezone.now().isoformat()
    cache = {}
    seen = to_augment = written = 0
    totals = {}
    first = last = None
    for snap in snaps.iterator(chunk_size=100):
        seen += 1
        doc = snap.state['components'][COMPONENT]
        data = doc['data']
        rows = data.get('entries') or []
        if not any(len(row) < WIDTH for row in rows):
            continue
        interval = data.get('interval') or {}
        key = (interval.get('start'), interval.get('end'))
        if key not in cache:
            cache[key] = _job_fields(
                row[0] for row in rows if len(row) < WIDTH)
        stats = _augment(rows, cache[key])
        to_augment += 1
        for field, (added, unresolved) in stats.items():
            a, u = totals.get(field) or (0, 0)
            totals[field] = (a + added, u + unresolved)
        first = first or snap.snap_time
        last = snap.snap_time
        if not args.apply or (args.limit and written >= args.limit):
            continue
        _note(doc, stats, now_iso)
        hashes = dict(snap.component_hashes or {})
        hashes[COMPONENT] = _canonical_hash(data)
        state_hash = _canonical_hash(snap.state['components'])
        with transaction.atomic():
            SystemSnap.objects.filter(pk=snap.pk).update(
                state=snap.state, component_hashes=hashes,
                state_hash=state_hash)
        written += 1

    print(f'live snaps carrying entries: {seen}; needing augmentation: '
          f'{to_augment} ({first} to {last}); distinct intervals looked '
          f'up: {len(cache)}')
    for field, (added, unresolved) in totals.items():
        print(f'  {field}: added to {added} rows; {unresolved} rows '
              f'without a job record (empty value)')
    if not args.apply:
        print('\ndry run — nothing written; --apply augments the snaps')
        return 0
    print(f'\napplied: augmented {written} snaps'
          + (f' (limit {args.limit})' if args.limit else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
