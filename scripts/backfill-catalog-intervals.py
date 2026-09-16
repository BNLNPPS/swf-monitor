#!/usr/bin/env python3
"""Backfill per-interval catalog snaps into snap history
(docs/SNAPPER_CATALOG.md, Backfill).

Reconstructs the catalog component's registrations group for every
five-minute interval from the digest every production job carries in
the PanDA record (payloadRegistration, payloadExit, payloadVersion in
jobmetrics), and the latency group from the payload reports at hand
(the metatable for finished jobs, the swept reports for failed ones).
One synthetic snap per non-empty interval is written under capture
policy ``backfill-catalog-v1``, reconstructed evidence distinguishable
from observed snaps, carrying only the catalog component in the live
publisher's envelope shape. The probe and registrar groups are absent
in a backfilled snap: the probe was not made at the time, and the
backlog is a gauge of the moment; the card and the curves treat an
absent group as not recorded, never as zero.

Idempotent: --apply first removes prior backfill-catalog-v1 snaps for
the scope, and writes only intervals ending strictly before the first
live catalog snap's interval start (or up to now when none exists).
Dry run by default.

Run under the venv with the swf-monitor project on the path:
    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-monitor>/scripts/backfill-catalog-intervals.py \\
        [--since 2026-09-15] [--step-minutes 5] [--apply]
"""
import argparse
import datetime as dt
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
import django  # noqa: E402
django.setup()

from django.utils import timezone  # noqa: E402

from monitor_app.snapper_catalog import (  # noqa: E402
    ASSESSMENT_POLICY_VERSION, CATALOG_REGISTRATION, COMPONENT_NAME,
    COMPONENT_SCHEMA_VERSION, PUBLISHER_IDENTITY, SCOPE, _ended_jobs,
    _iso_utc, _latency_rows, latency_from_rows, registrations_from_rows,
)
from snapper_ai.models import SystemSnap  # noqa: E402

CAPTURE_POLICY = 'backfill-catalog-v1'


def _interval_snaps(start, end, step):
    out = []
    lead = start
    while lead < end:
        upto = min(lead + step, end)
        rows = _ended_jobs(lead, upto)
        if rows:
            registrations = registrations_from_rows(rows)
            latency = latency_from_rows(_latency_rows(lead, upto))
            out.append((lead, upto, registrations, latency))
        lead = upto
    return out


def main():
    parser = argparse.ArgumentParser(
        description='Backfill per-interval catalog snaps into epicprod snap history.')
    parser.add_argument('--since', default='2026-09-15',
                        help='first day to reconstruct (ET midnight), default 2026-09-15')
    parser.add_argument('--step-minutes', type=int, default=5)
    parser.add_argument('--apply', action='store_true', help='write the snaps (dry run without)')
    args = parser.parse_args()

    now = timezone.now()
    step = dt.timedelta(minutes=args.step_minutes)
    live_first = (SystemSnap.objects
                  .filter(scope=SCOPE, state__components__has_key=COMPONENT_NAME)
                  .exclude(capture_policy=CAPTURE_POLICY)
                  .order_by('snap_time').values('state').first())
    if live_first:
        seam_iso = live_first['state']['components'][COMPONENT_NAME]['data']['interval']['start']
        seam = dt.datetime.fromisoformat(seam_iso.replace('Z', '+00:00'))
    else:
        seam = now
    from zoneinfo import ZoneInfo
    start = dt.datetime.strptime(args.since, '%Y-%m-%d').replace(tzinfo=ZoneInfo('America/New_York'))
    start = start.astimezone(dt.timezone.utc)
    grid_end = seam.replace(second=0, microsecond=0)
    grid_end -= dt.timedelta(minutes=grid_end.minute % args.step_minutes)
    print(f'grid: {args.step_minutes}m step, {start.isoformat()} -> {grid_end.isoformat()}')
    print('seam: ' + (f'first live catalog interval starts {seam.isoformat()}' if live_first
                      else 'now (no live catalog snap yet)'))

    snaps = []
    day = start
    while day < grid_end:
        day_end = min(day + dt.timedelta(days=1), grid_end)
        snaps.extend(_interval_snaps(day, day_end, step))
        day = day_end
    if live_first and seam > grid_end:
        snaps.extend(_interval_snaps(grid_end, seam, step))

    total = sum(r['jobs'] for _, _, r, _ in snaps)
    lost = sum((r['by_outcome'] or {}).get('failed', 0) for _, _, r, _ in snaps)
    print(f'non-empty intervals: {len(snaps)}, jobs {total}, registrations lost {lost}')
    for lead, upto, r, lat in sorted(snaps, key=lambda s: -(s[2]['by_outcome'].get('failed', 0)))[:3]:
        print(f'  {upto.isoformat()}: {r["jobs"]} jobs, {r["by_outcome"]}, exits {r["failed_by_exit"]}, '
              f'registered median {(lat.get("registered") or {}).get("median_s")} s')
    if not args.apply:
        print('\ndry run; nothing written; --apply writes the snaps')
        return 0

    removed = SystemSnap.objects.filter(scope=SCOPE, capture_policy=CAPTURE_POLICY).delete()
    written = 0
    for lead, upto, registrations, latency in snaps:
        SystemSnap.objects.create(
            scope=SCOPE,
            # Three seconds past the grid instant: the errors backfill
            # holds +2 s and the live captures the aligned boundaries.
            snap_time=upto + dt.timedelta(seconds=3),
            observed_at=now, completed_at=now, snap_schema_version=1,
            capture_policy=CAPTURE_POLICY, encoding='full', reasons=['backfill'],
            changed_components=[COMPONENT_NAME],
            component_revisions={COMPONENT_NAME: 0},
            registration_versions={COMPONENT_NAME: COMPONENT_SCHEMA_VERSION},
            component_hashes={}, state_hash='',
            state={'components': {COMPONENT_NAME: {
                'v': COMPONENT_SCHEMA_VERSION,
                'data': {'interval': {'start': _iso_utc(lead), 'end': _iso_utc(upto)},
                         'registrations': registrations, 'latency': latency,
                         'backfilled': True},
                'registration': CATALOG_REGISTRATION,
                'revision': 0,
                'registration_version': COMPONENT_SCHEMA_VERSION,
                'assessed_at': upto.isoformat(), 'source_as_of': upto.isoformat(),
                'accepted_at': now.isoformat(),
                'assessment_policy': ASSESSMENT_POLICY_VERSION,
                'publisher_identity': PUBLISHER_IDENTITY,
            }}},
        )
        written += 1
    print(f'\napplied: removed prior backfill {removed[0]}, wrote {written} snaps')
    return 0


if __name__ == '__main__':
    sys.exit(main())
