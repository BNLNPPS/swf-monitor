"""Terminalize RunState rows abandoned by their writer — one-time repair.

Two strata of stale state, both repaired here. Rows whose Run record
carries an end_time — the pre-transition-processor era, when RunState
kept its launch state forever — become ended/completed at the run's
true end. Rows whose run was announced but never started or ended (no
Run end_time, non-terminal state, stale beyond --hours) — the residue
of crashed launchers — become abandoned/failed at their last
transition. The runner now terminalizes on every exit path, and the
stale-state System check names any future survivor; this command is
repair, never a schedule. Dry run by default; --apply writes.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from monitor_app.models import Run, RunState


TERMINAL_STATES = ('ended', 'expired', 'abandoned')


class Command(BaseCommand):
    help = ("Mark RunState rows ended when their Run has an end_time. "
            "Dry run unless --apply is given.")

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write the repairs (default is a dry-run listing).')
        parser.add_argument(
            '--hours', type=float, default=12,
            help='staleness threshold for never-ended runs')

    def handle(self, *args, **options):
        ended_runs = dict(
            Run.objects.filter(end_time__isnull=False)
            .values_list('run_number', 'end_time'))
        cutoff = timezone.now() - timedelta(hours=options['hours'])
        candidates = []
        abandoned = []
        for row in RunState.objects.exclude(
                state__in=TERMINAL_STATES).order_by('run_number'):
            if row.run_number in ended_runs:
                candidates.append(row)
            elif row.state_changed_at < cutoff:
                abandoned.append(row)
        if not candidates and not abandoned:
            self.stdout.write('No stuck RunState rows found.')
            return

        for row in candidates:
            self.stdout.write(
                f"run {row.run_number}: {row.state}/{row.substate or '-'} "
                f"({row.phase}) since {row.state_changed_at:%Y-%m-%d %H:%M} "
                f"— run ended {ended_runs[row.run_number]:%Y-%m-%d %H:%M}")
        for row in abandoned:
            self.stdout.write(
                f"run {row.run_number}: {row.state}/{row.substate or '-'} "
                f"({row.phase}) since {row.state_changed_at:%Y-%m-%d %H:%M} "
                f"— announced, never ended -> abandoned")
        self.stdout.write(
            f'{len(candidates)} ended-run row(s), '
            f'{len(abandoned)} never-ended row(s).')

        if not options['apply']:
            self.stdout.write('Dry run — nothing written. '
                              'Re-run with --apply to repair.')
            return

        for row in candidates:
            row.state = 'ended'
            row.substate = None
            row.phase = 'completed'
            row.state_changed_at = ended_runs[row.run_number]
            row.save(update_fields=[
                'state', 'substate', 'phase', 'state_changed_at',
                'updated_at'])
        for row in abandoned:
            row.state = 'abandoned'
            row.substate = None
            row.phase = 'failed'
            row.save(update_fields=[
                'state', 'substate', 'phase', 'updated_at'])
        self.stdout.write(self.style.SUCCESS(
            f'Repaired {len(candidates) + len(abandoned)} '
            'RunState row(s).'))

        # The lanes read the published datataking component, not RunState
        # directly — a repair is not done until the surface reflects it.
        from monitor_app.snapper_datataking import publish_datataking_state
        publish_datataking_state()
        self.stdout.write('Datataking component republished.')
