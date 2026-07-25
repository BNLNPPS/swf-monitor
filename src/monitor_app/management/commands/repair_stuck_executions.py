"""Terminalize workflow-execution rows abandoned as 'running'.

Before the runner finalized its execution record on every exit path
(swf-testbed workflow_runner.py, 2026-07-24), a crashed or killed run
left its row claiming 'running' forever, and every surface reading the
record repeated the lie ("26 executions running" beside all-idle
lanes). This one-shot repair marks such rows terminated, with the end
time taken from the run record's last activity for the execution's run
when one exists, else the execution's own start time. The runner fix
prevents new ones; there is no scheduled janitor — a system needing
one is writing garbage.

Dry-run by default; --apply writes. A row is stuck when status is
'running' with no end_time and a start older than --hours (default 12).
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models.fields.json import KeyTextTransform
from django.utils import timezone

from monitor_app.models import RunState, SystemStateEvent
from monitor_app.workflow_models import WorkflowExecution


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='write the repair (default: report only)')
        parser.add_argument('--hours', type=float, default=12,
                            help='running-with-no-end age threshold')

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(hours=options['hours'])
        stuck = list(
            WorkflowExecution.objects
            .filter(status='running', end_time__isnull=True,
                    start_time__lt=cutoff)
            .order_by('start_time'))
        execution_runs = {}
        for run_number, execution_key in (
                RunState.objects
                .annotate(execution_key=KeyTextTransform(
                    'execution_id', 'metadata'))
                .exclude(execution_key__isnull=True)
                .values_list('run_number', 'execution_key')):
            execution_runs.setdefault(execution_key, run_number)

        repaired = 0
        for execution in stuck:
            run_number = execution_runs.get(execution.execution_id)
            last_activity = None
            if run_number is not None:
                last_activity = (
                    SystemStateEvent.objects
                    .filter(run_number=run_number)
                    .order_by('-timestamp')
                    .values_list('timestamp', flat=True)
                    .first())
            end_time = last_activity or execution.start_time
            self.stdout.write(
                f'{execution.execution_id}: running since '
                f'{execution.start_time:%Y-%m-%d %H:%M} -> terminated '
                f'at {end_time:%Y-%m-%d %H:%M}')
            if options['apply']:
                execution.status = 'terminated'
                execution.end_time = end_time
                execution.save(update_fields=['status', 'end_time'])
            repaired += 1

        mode = 'APPLIED' if options['apply'] else 'DRY RUN'
        self.stdout.write(f'{mode}: {repaired} stuck executions')
