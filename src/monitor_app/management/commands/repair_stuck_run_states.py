"""Terminalize RunState rows whose run has ended — one-time repair.

Before the ActiveMQ processor applied run lifecycle transitions
(run_state_transitions.py), RunState rows kept their launch-time state
forever, so ended runs still read as active on the Snapper datataking
lanes. This command marks rows terminal when the corresponding Run
record carries an end_time. Dry run by default; --apply writes.
"""

from django.core.management.base import BaseCommand

from monitor_app.models import Run, RunState


TERMINAL_STATES = ('ended',)


class Command(BaseCommand):
    help = ("Mark RunState rows ended when their Run has an end_time. "
            "Dry run unless --apply is given.")

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write the repairs (default is a dry-run listing).')

    def handle(self, *args, **options):
        ended_runs = dict(
            Run.objects.filter(end_time__isnull=False)
            .values_list('run_number', 'end_time'))
        candidates = [
            row for row in RunState.objects.exclude(
                state__in=TERMINAL_STATES).order_by('run_number')
            if row.run_number in ended_runs
        ]
        if not candidates:
            self.stdout.write('No stuck RunState rows found.')
            return

        for row in candidates:
            self.stdout.write(
                f"run {row.run_number}: {row.state}/{row.substate or '-'} "
                f"({row.phase}) since {row.state_changed_at:%Y-%m-%d %H:%M} "
                f"— run ended {ended_runs[row.run_number]:%Y-%m-%d %H:%M}")
        self.stdout.write(f'{len(candidates)} stuck row(s).')

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
        self.stdout.write(self.style.SUCCESS(
            f'Repaired {len(candidates)} RunState row(s).'))

        # The lanes read the published datataking component, not RunState
        # directly — a repair is not done until the surface reflects it.
        from monitor_app.snapper_datataking import publish_datataking_state
        publish_datataking_state()
        self.stdout.write('Datataking component republished.')
