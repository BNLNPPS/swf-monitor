"""Backfill per-interval error-event snaps into snap history.

Reconstructs the errors component's interval entries at the current
schema version — every job that ended faulty within each grid
interval, as rows of (pandaid, jeditaskid, category, endtime, status,
exitcode, held) — from the recorded
job history in the PanDA database, on a regular grid at the live
capture cadence. One synthetic snap per non-empty interval is written with
capture policy ``backfill-errors-v1`` — reconstructed evidence,
explicitly distinguishable from observed snaps — carrying only the
errors component in the live publisher's envelope shape. A job reports
errors once, upon completion, so each failed job lands in exactly one
interval; the backfilled intervals and the live interval chain form
one consistent record. Empty intervals write nothing, matching the
live quiet behavior.

Idempotent: --apply first removes prior backfill-errors-v1 snaps for
the scope, and writes only intervals ending strictly before the
earliest live snap carrying version-2 entries (or up to now when none
exists yet). Earlier version-1 counter snaps are ignored as a
boundary: the entry record supersedes them over the same span.
Dry-run default.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-monitor>/scripts/backfill-errors-entries.py \\
        [--days 30] [--step-minutes 5] [--apply]
"""

import argparse
import datetime as dt
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from django.utils import timezone  # noqa: E402

from monitor_app.snapper_errors import (  # noqa: E402
    ASSESSMENT_POLICY_VERSION,
    COMPONENT_SCHEMA_VERSION,
    ERRORS_REGISTRATION,
    MAX_ENTRIES,
    PUBLISHER_IDENTITY,
    _category_key,
    _entry_rows,
    _exit_key,
    _iso_utc,
)
from snapper_ai.models import SystemSnap  # noqa: E402

CAPTURE_POLICY = 'backfill-errors-v1'
SCOPE = 'epicprod'
COMPONENT = 'errors'


def _interval_snaps(day_start, day_end, step):
    """(interval_start, interval_end, entries, overflow) per non-empty
    grid interval in one slice, from one query over the job records.
    Interval ends are grid edges capped at the slice end, so a partial
    final interval closes exactly at day_end."""
    rows = _entry_rows(day_start, day_end)
    intervals = []
    lead = day_start
    edge = min(day_start + step, day_end)
    entries = []
    overflow_total = 0
    overflow_categories = {}
    overflow_held = {}

    def close():
        nonlocal entries, overflow_total, overflow_categories, overflow_held
        if entries:
            overflow = (
                {'total': overflow_total,
                 'by_category': overflow_categories,
                 'held_by_category': overflow_held}
                if overflow_total else None
            )
            intervals.append((lead, edge, list(entries), overflow))
        entries = []
        overflow_total = 0
        overflow_categories = {}
        overflow_held = {}

    for (pandaid, taskid, comp, code, endtime, status, exitcode,
         held) in rows:
        stamp = endtime if endtime.tzinfo else endtime.replace(
            tzinfo=dt.timezone.utc)
        while stamp > edge:
            close()
            lead = edge
            edge = min(edge + step, day_end)
        category = _category_key(comp, code)
        if len(entries) < MAX_ENTRIES:
            entries.append([
                int(pandaid or 0),
                int(taskid or 0),
                category,
                _iso_utc(endtime),
                str(status or ''),
                _exit_key(exitcode),
                int(held or 0),
            ])
        else:
            overflow_total += 1
            fold_key = f'{category}@{status or ""}@{_exit_key(exitcode)}'
            overflow_categories[fold_key] = (
                overflow_categories.get(fold_key) or 0) + 1
            overflow_held[fold_key] = (
                overflow_held.get(fold_key) or 0) + int(held or 0)
    close()
    return intervals


def main():
    parser = argparse.ArgumentParser(
        description='Backfill per-interval error-event snaps into '
                    'epicprod snap history.')
    parser.add_argument('--days', type=int, default=30,
                        help='trailing horizon in days (default 30)')
    parser.add_argument('--step-minutes', type=int, default=5,
                        help='grid spacing in minutes (default 5, the '
                             'live capture cadence)')
    parser.add_argument('--apply', action='store_true',
                        help='write the snaps (dry run without)')
    args = parser.parse_args()

    now = timezone.now()
    step = dt.timedelta(minutes=args.step_minutes)

    # The seam is the START of the first live entries interval: the
    # backfilled record must tile exactly against the live chain, every
    # job in exactly one interval. Until the version-2 maintainer has
    # published (deploy first, then backfill), the seam falls back to
    # now — fine for a dry run, re-run after deploy for the real seam.
    live_first = (
        SystemSnap.objects
        .filter(scope=SCOPE,
                state__components__errors__data__has_key='entries')
        .exclude(capture_policy=CAPTURE_POLICY)
        .order_by('snap_time')
        .values('state')
        .first())
    if live_first:
        seam_iso = (live_first['state']['components']['errors']['data']
                    ['interval']['start'])
        seam = dt.datetime.fromisoformat(seam_iso.replace('Z', '+00:00'))
    else:
        seam = now
    start = seam - dt.timedelta(days=args.days)
    # Align the grid to round step boundaries.
    start = start.replace(second=0, microsecond=0)
    start -= dt.timedelta(minutes=start.minute % args.step_minutes)
    grid_end = seam.replace(second=0, microsecond=0)
    grid_end -= dt.timedelta(minutes=grid_end.minute % args.step_minutes)

    print(f'grid: {args.step_minutes}m step, '
          f'{start.isoformat()} -> {grid_end.isoformat()}')
    print(f'seam: '
          f'{"first live entries interval starts " + seam.isoformat() if live_first else "now (no live entries snap yet — deploy first, then re-run)"}')

    snaps = []
    day = start
    while day < grid_end:
        day_end = min(day + dt.timedelta(days=1), grid_end)
        snaps.extend(_interval_snaps(day, day_end, step))
        day = day_end
    if live_first and seam > grid_end:
        # One final partial interval closes the record exactly at the
        # live chain's first interval start.
        snaps.extend(_interval_snaps(grid_end, seam, step))

    total_entries = sum(len(entries) for _, _, entries, _ in snaps)
    total_overflow = sum(
        (overflow or {}).get('total') or 0 for _, _, _, overflow in snaps)
    busiest = max(snaps, key=lambda s: len(s[2]) + ((s[3] or {}).get('total') or 0),
                  default=None)
    print(f'non-empty intervals: {len(snaps)}, entries {total_entries}, '
          f'overflow {total_overflow}')
    if busiest:
        _, end_stamp, entries, overflow = busiest
        print(f'busiest interval ends {end_stamp.isoformat()}: '
              f'{len(entries)} entries'
              + (f' + {overflow["total"]} overflow' if overflow else ''))
    for _, end_stamp, entries, overflow in snaps[-3:]:
        categories = {}
        for row in entries:
            categories[row[2]] = (categories.get(row[2]) or 0) + 1
        top = sorted(categories.items(), key=lambda kv: -kv[1])[:3]
        print(f'  {end_stamp.isoformat()}: {len(entries)} entries, top {top}')

    if not args.apply:
        print('\ndry run — nothing written; --apply writes the snaps')
        return 0

    removed = SystemSnap.objects.filter(
        scope=SCOPE, capture_policy=CAPTURE_POLICY).delete()
    written = 0
    for lead, end_stamp, entries, overflow in snaps:
        # Two seconds past the grid instant: live captures land on
        # aligned 30-second boundaries and the hourly counter backfill
        # (backfill-panda-counters.py) owns the one-second offset, so
        # this stamp never collides under the (scope, snap_time)
        # uniqueness.
        SystemSnap.objects.create(
            scope=SCOPE,
            snap_time=end_stamp + dt.timedelta(seconds=2),
            observed_at=now,
            completed_at=now,
            snap_schema_version=1,
            capture_policy=CAPTURE_POLICY,
            encoding='full',
            reasons=['backfill'],
            changed_components=[COMPONENT],
            component_revisions={COMPONENT: 0},
            registration_versions={COMPONENT: COMPONENT_SCHEMA_VERSION},
            component_hashes={},
            state_hash='',
            state={'components': {COMPONENT: {
                'v': COMPONENT_SCHEMA_VERSION,
                'data': dict(
                    {'interval': {'start': _iso_utc(lead),
                                  'end': _iso_utc(end_stamp)},
                     'entries': entries},
                    **({'overflow': overflow} if overflow else {})),
                'registration': ERRORS_REGISTRATION,
                'revision': 0,
                'registration_version': COMPONENT_SCHEMA_VERSION,
                'assessed_at': end_stamp.isoformat(),
                'source_as_of': end_stamp.isoformat(),
                'accepted_at': now.isoformat(),
                'assessment_policy': ASSESSMENT_POLICY_VERSION,
                'publisher_identity': PUBLISHER_IDENTITY,
            }}},
        )
        written += 1
    print(f'\napplied: removed prior backfill {removed[0]}, '
          f'wrote {written} snaps')
    return 0


if __name__ == '__main__':
    sys.exit(main())
