"""Align recorded snap datataking history with the run record.

Before the E0-E1 run-state write side went live (2026-07-23), RunState
rows sat at their launch state through entire runs, and Snapper honestly
recorded that wrong bookkeeping into snap history: a run's physics
window reads ``imminent/preparing`` in the snaps of that era, so the
Time history's cut contradicts the activity lanes. By operator decision
(2026-07-24, "fix the history"), this command rewrites each snap's
datataking namespace entries to what the run record says was true at
that snap's instant:

- inside the run window (Run.start_time..end_time): phase ``physics``,
  state ``running``, substate ``physics``, transition at the run start;
- after the run window: phase ``completed``, state ``ended``, no
  substate, transition at the run end;
- before the run window the recorded pre-run state stands, and entries
  whose run has no recorded end are left untouched.

The rewrite is compare-and-set and idempotent: entries already agreeing
with the run record are never touched, so correctly recorded eras pass
through unchanged. Standby interludes inside the window are not
reconstructed — simulated runs pause for seconds, below snap cadence.

Dry-run by default; --apply writes.
"""

from django.core.management.base import BaseCommand

from monitor_app.models import Run
from snapper_ai.models import SystemSnap


def _iso_z(value):
    return value.isoformat().replace('+00:00', 'Z')


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='write the repair (default: report only)')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        runs = {row['run_number']: row for row in
                Run.objects.exclude(end_time__isnull=True)
                .values('run_number', 'start_time', 'end_time')}
        snaps_changed = 0
        entries_changed = 0
        by_kind = {'physics': 0, 'ended': 0}

        queryset = (SystemSnap.objects.filter(scope='testbed')
                    .order_by('snap_time').iterator(chunk_size=200))
        for snap in queryset:
            state = snap.state if isinstance(snap.state, dict) else {}
            namespaces = (((state.get('components') or {})
                           .get('datataking') or {})
                          .get('data') or {}).get('namespaces')
            if not isinstance(namespaces, dict):
                continue
            snap_dirty = False
            for namespace, entry in namespaces.items():
                if not isinstance(entry, dict):
                    continue
                run = runs.get(entry.get('run_number'))
                if run is None:
                    continue
                if snap.snap_time < run['start_time']:
                    continue
                if snap.snap_time <= run['end_time']:
                    wanted = {'phase': 'physics', 'state': 'running',
                              'substate': 'physics',
                              'last_transition_at':
                                  _iso_z(run['start_time'])}
                    kind = 'physics'
                else:
                    wanted = {'phase': 'completed', 'state': 'ended',
                              'last_transition_at':
                                  _iso_z(run['end_time'])}
                    kind = 'ended'
                current = {key: entry.get(key) for key in wanted}
                current_substate = entry.get('substate')
                needs_substate_drop = (kind == 'ended'
                                       and current_substate is not None)
                if current == wanted and not needs_substate_drop:
                    continue
                entry.update(wanted)
                if kind == 'ended':
                    entry.pop('substate', None)
                entries_changed += 1
                by_kind[kind] += 1
                snap_dirty = True
            if snap_dirty:
                snaps_changed += 1
                if apply_changes:
                    snap.save(update_fields=['state'])

        mode = 'APPLIED' if apply_changes else 'DRY RUN'
        self.stdout.write(
            f'{mode}: {entries_changed} namespace entries in '
            f'{snaps_changed} snaps '
            f"({by_kind['physics']} to physics, {by_kind['ended']} to "
            f'ended); runs with a recorded end considered: {len(runs)}')
