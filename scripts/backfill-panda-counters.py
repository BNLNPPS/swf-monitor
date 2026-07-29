"""Backfill cumulative terminal-outcome counters into snap history.

Reconstructs the panda component's version-5 cumulative terminal
counters (jobs.cum, per-site cum and cum_failed_by_class) at a regular
grid of historical instants, from the complete recorded job history in
the PanDA database. One synthetic snap per grid instant is written
with capture policy ``backfill-panda-v1`` — reconstructed evidence,
explicitly distinguishable from observed snaps — carrying only the
counter fields in the live publisher's envelope shape. The counters
are absolute counts of terminal events with end times at or before
each instant, the same origin the live publisher seeds from, so the
backfilled grid and the live counter chain form one consistent record.

Idempotent: --apply first removes prior backfill-panda-v1 snaps for
the scope, and writes only instants strictly before the earliest live
snap carrying jobs.cum (or up to now when none exists yet). Dry-run
default.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-monitor>/scripts/backfill-panda-counters.py \\
        [--start 2026-07-01] [--step-hours 1] [--apply]
"""

import argparse
import datetime as dt
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from django.db import connections  # noqa: E402
from django.utils import timezone  # noqa: E402

from monitor_app.panda.constants import (  # noqa: E402
    ERROR_COMPONENTS,
    PANDA_SCHEMA,
)
from monitor_app.snapper_panda import (  # noqa: E402
    ASSESSMENT_POLICY_VERSION,
    MAX_SITES,
    PANDA_REGISTRATION,
    PUBLISHER_IDENTITY,
    TERMINAL_JOB_STATUSES,
)
from snapper_ai.models import SystemSnap  # noqa: E402

CAPTURE_POLICY = 'backfill-panda-v1'


def _hourly_outcome_buckets(until):
    """Terminal-outcome counts per (hour bucket, site, status, failure
    class) over the full recorded history up to ``until``, deduplicated
    across the active and archived tables."""
    class_case = ' '.join(
        f'WHEN "{c["code"]}" > 0 THEN \'{c["name"]}\''
        for c in ERROR_COMPONENTS
    )
    err_fields = ', '.join(f'"{c["code"]}"' for c in ERROR_COMPONENTS)
    placeholders = ', '.join(['%s'] * len(TERMINAL_JOB_STATUSES))
    bounds = f'"endtime" <= %s AND "jobstatus" IN ({placeholders})'
    params = [until, *TERMINAL_JOB_STATUSES]
    sql = f"""
        SELECT date_trunc('hour', "endtime") AS bucket,
               COALESCE("computingsite", 'unknown'), "jobstatus",
               CASE WHEN "jobstatus" = 'failed'
                    THEN CASE {class_case} ELSE 'other' END
                    ELSE '' END,
               COUNT(*)
        FROM (
            SELECT "pandaid", "endtime", "jobstatus",
                   "computingsite", {err_fields}
            FROM "{PANDA_SCHEMA}"."jobsactive4" WHERE {bounds}
            UNION
            SELECT "pandaid", "endtime", "jobstatus",
                   "computingsite", {err_fields}
            FROM "{PANDA_SCHEMA}"."jobsarchived4" WHERE {bounds}
        ) completed
        GROUP BY 1, 2, 3, 4
        ORDER BY 1
    """
    with connections['panda'].cursor() as cursor:
        cursor.execute(sql, params + params)
        return cursor.fetchall()


def _bump(counter, key, count):
    counter[key] = int(counter.get(key) or 0) + count


def _counters_at_instants(instants, buckets):
    """Walk the hour buckets once, emitting deep-copied counter maps at
    each grid instant. Bucket times are naive UTC from the database;
    a bucket belongs to instant t when the whole hour ends at or
    before t."""
    scope_cum = {}
    site_cums = {}
    results = []
    index = 0
    for instant in instants:
        cutoff = instant.replace(tzinfo=None)
        while index < len(buckets):
            bucket, site, status, fclass, count = buckets[index]
            if bucket + dt.timedelta(hours=1) > cutoff:
                break
            count = int(count or 0)
            _bump(scope_cum, status, count)
            entry = site_cums.setdefault(
                str(site or 'unknown'), {'cum': {}, 'classes': {}})
            _bump(entry['cum'], status, count)
            if status == 'failed' and fclass:
                _bump(entry['classes'], fclass, count)
            index += 1
        ranked = sorted(
            site_cums,
            key=lambda name: (-sum(site_cums[name]['cum'].values()), name),
        )[:MAX_SITES]
        sites = {}
        for name in ranked:
            entry = site_cums[name]
            block = {'cum': dict(entry['cum'])}
            if entry['classes']:
                block['cum_failed_by_class'] = dict(entry['classes'])
            sites[name] = block
        results.append((instant, dict(scope_cum), sites))
    return results


def main():
    parser = argparse.ArgumentParser(
        description='Backfill cumulative terminal-outcome counters '
                    'into epicprod snap history.')
    parser.add_argument('--start', default='2026-07-01',
                        help='first grid instant, UTC date or ISO '
                             '(default 2026-07-01)')
    parser.add_argument('--step-hours', type=float, default=1.0,
                        help='grid spacing in hours (default 1)')
    parser.add_argument('--apply', action='store_true',
                        help='write the snaps (dry run without)')
    args = parser.parse_args()

    start = dt.datetime.fromisoformat(args.start)
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.timezone.utc)
    now = timezone.now()

    live_first = (
        SystemSnap.objects
        .filter(scope='epicprod',
                state__components__panda__data__jobs__has_key='cum')
        .exclude(capture_policy=CAPTURE_POLICY)
        .order_by('snap_time')
        .values_list('snap_time', flat=True)
        .first())
    end = live_first or now
    if start >= end:
        print(f'nothing to do: start {start} is not before end {end}')
        return 0

    instants = []
    step = dt.timedelta(hours=args.step_hours)
    instant = start
    while instant < end:
        instants.append(instant)
        instant = instant + step

    buckets = _hourly_outcome_buckets(end)
    results = _counters_at_instants(instants, buckets)

    print(f'grid: {len(instants)} instants, {args.step_hours}h step, '
          f'{start.isoformat()} -> {instants[-1].isoformat()}')
    print(f'end boundary: '
          f'{"earliest live counter snap " + end.isoformat() if live_first else "now"}')
    print(f'hour buckets: {len(buckets)}')
    for instant, scope_cum, sites in results[-3:]:
        google = sites.get('BNL_ePIC_GOOGLE') or {}
        print(f'  {instant.isoformat()}: scope {scope_cum} | '
              f'BNL_ePIC_GOOGLE {google.get("cum")} '
              f'{google.get("cum_failed_by_class")}')

    if not args.apply:
        print('\ndry run — nothing written; --apply writes the snaps')
        return 0

    removed = SystemSnap.objects.filter(
        scope='epicprod', capture_policy=CAPTURE_POLICY).delete()
    written = 0
    for instant, scope_cum, sites in results:
        SystemSnap.objects.create(
            scope='epicprod',
            snap_time=instant,
            observed_at=now,
            completed_at=now,
            snap_schema_version=1,
            capture_policy=CAPTURE_POLICY,
            encoding='full',
            reasons=['backfill'],
            changed_components=['panda'],
            component_revisions={'panda': 0},
            registration_versions={'panda': 5},
            component_hashes={},
            state_hash='',
            state={'components': {'panda': {
                'v': 1,
                'data': {'jobs': {'cum': scope_cum, 'sites': sites}},
                'registration': PANDA_REGISTRATION,
                'revision': 0,
                'registration_version': 5,
                'assessed_at': instant.isoformat(),
                'source_as_of': instant.isoformat(),
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
