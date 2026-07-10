import json
import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, ProgrammingError

from monitor_app.epicprod_inventory import (
    build_expected_files_for_task,
    sync_expected_files_for_task,
    sync_job_from_study_data,
)
from monitor_app.panda.queries import study_job


CACHE_PAYLOAD_LOG = (
    Path(__file__).resolve().parents[4] / 'scripts' / 'cache-payload-log.py'
)
STUDY_FETCH_TIMEOUT = int(os.environ.get('EPICPROD_STUDY_FETCH_TIMEOUT', '150'))


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
            from pcs.models import PandaTasks
            assoc = (
                PandaTasks.objects
                .select_related('prod_task', 'prod_task__dataset', 'prod_task__prod_config')
                .filter(jedi_task_id=jeditaskid)
                .first()
            )
            task = assoc.prod_task if assoc else qs.filter(panda_task_id=jeditaskid).first()
            if not task:
                raise CommandError(f'No PCS task association records jediTaskID={jeditaskid}')
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
        if not dry_run:
            self._cache_payload_log_before_study(pandaid, data)
        if dry_run:
            self.stdout.write(json.dumps(self._jsonable(data), indent=2))
            return
        job = sync_job_from_study_data(data)
        self.stdout.write(
            self.style.SUCCESS(
                f'synced epicprod job {job.pandaid}: phase={job.phase or "(none)"}'
            )
        )

    def _cache_payload_log_before_study(self, pandaid, data):
        job = data.get('job') or {}
        log_file = data.get('log_file') or {}
        jeditaskid = job.get('jeditaskid')
        scope = log_file.get('scope')
        lfn = log_file.get('lfn')
        if not (jeditaskid and scope and lfn):
            return
        cache_root = getattr(settings, 'SWF_TMP_DIR', '/data/swf-tmp')
        done = os.path.join(cache_root, 'panda-logs', str(jeditaskid), str(pandaid), '.done')
        if os.path.isfile(done):
            return
        cmd = [
            sys.executable, str(CACHE_PAYLOAD_LOG),
            '--scope', str(scope),
            '--lfn', str(lfn),
            '--jeditaskid', str(jeditaskid),
            '--pandaid', str(pandaid),
        ]
        self.stdout.write(f'caching payload log before study: pandaid={pandaid}')
        try:
            p = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=STUDY_FETCH_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandError(
                f'payload log fetch timed out before study for pandaid={pandaid} '
                f'after {STUDY_FETCH_TIMEOUT}s'
            ) from exc
        if p.returncode != 0:
            stderr = (p.stderr or '').strip()
            reason = stderr.splitlines()[-1] if stderr else f'rc={p.returncode}'
            raise CommandError(
                f'payload log fetch failed before study for pandaid={pandaid}: {reason}'
            )
        for line in (p.stderr or '').splitlines():
            self.stdout.write(f'  cache-payload-log: {line}')

    @staticmethod
    def _jsonable(value):
        return json.loads(json.dumps(value, default=str))
