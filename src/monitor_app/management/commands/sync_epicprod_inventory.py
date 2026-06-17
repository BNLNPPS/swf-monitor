import json

from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, ProgrammingError

from monitor_app.epicprod_inventory import (
    build_expected_files_for_task,
    sync_expected_files_for_task,
    sync_job_from_study_data,
)
from monitor_app.panda.queries import study_job


class Command(BaseCommand):
    help = "Build or refresh ePIC production job/file inventory."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--pandaid', type=int, help='PanDA job id to refresh')
        group.add_argument('--jeditaskid', type=int, help='JEDI task id to build expected files for')
        group.add_argument('--prod-task', help='PCS task name/composed name to build expected files for')
        parser.add_argument('--spec-file',
                            help='EVGEN spec JSON to use as the expected-file source')
        parser.add_argument('--dry-run', action='store_true',
                            help='Print derived rows without writing database changes')

    def handle(self, *args, **options):
        try:
            if options['pandaid']:
                self._sync_pandaid(options['pandaid'], options['dry_run'])
            else:
                task = self._resolve_task(
                    jeditaskid=options.get('jeditaskid'),
                    name=options.get('prod_task'),
                )
                spec = None
                if options.get('spec_file'):
                    with open(options['spec_file']) as f:
                        spec = json.load(f)
                self._sync_task(task, options['dry_run'], spec=spec)
        except (OperationalError, ProgrammingError) as exc:
            raise CommandError(
                f'epicprod inventory tables are not available; run migrations first: {exc}'
            ) from exc

    def _resolve_task(self, *, jeditaskid=None, name=None):
        from pcs.models import ProdTask
        qs = ProdTask.objects.select_related('dataset', 'prod_config')
        if jeditaskid:
            task = qs.filter(panda_task_id=jeditaskid).first()
            if not task:
                raise CommandError(f'No PCS task records panda_task_id={jeditaskid}')
            return task
        task = qs.filter(name=name).first()
        if not task:
            for t in qs.all():
                if t.composed_name == name:
                    return t
            raise CommandError(f'No PCS task found for {name!r}')
        return task

    def _sync_task(self, task, dry_run, spec=None):
        if dry_run:
            rows = build_expected_files_for_task(task, spec=spec)
            self.stdout.write(json.dumps(self._jsonable(rows), indent=2))
            return
        rows = sync_expected_files_for_task(task, spec=spec)
        self.stdout.write(
            self.style.SUCCESS(
                f'synced {len(rows)} expected file row(s) for {task.composed_name}'
            )
        )

    def _sync_pandaid(self, pandaid, dry_run):
        data = study_job(pandaid)
        if 'error' in data:
            raise CommandError(data['error'])
        if dry_run:
            self.stdout.write(json.dumps(self._jsonable(data), indent=2))
            return
        job = sync_job_from_study_data(data)
        self.stdout.write(
            self.style.SUCCESS(
                f'synced epicprod job {job.pandaid}: phase={job.phase or "(none)"}'
            )
        )

    @staticmethod
    def _jsonable(value):
        return json.loads(json.dumps(value, default=str))
