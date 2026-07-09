"""Backfill legacy AIContent assessments into corun-ai Pages."""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ai.assessments import (
    AI_CONTENT_COMMENT_KEY,
    AI_CONTENT_QUALITY_KEY,
    CORUN_ASSESSMENT_SECTION,
    append_corun_page_group_id,
    corun_page_group_ids,
)
from ai.corun_client import CorunAPIError, CorunClient, corun_configured
from monitor_app.models import AIContent, EpicProdJob, PandaQueue


CORUN_MAPPING_KEY = 'corun_page_group_id'


class Command(BaseCommand):
    help = 'Backfill legacy AIContent rows into corun-ai Pages.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be created/linked without writing corun-ai or local DB changes.',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=0,
            help='Maximum number of AIContent rows to process; 0 means all.',
        )
        parser.add_argument(
            '--id',
            dest='ids',
            type=int,
            action='append',
            default=[],
            help='Backfill one AIContent id. May be repeated.',
        )

    def handle(self, *args, **options):
        if not corun_configured() and not options['dry_run']:
            raise CommandError('CORUN_BASE_URL and CORUN_API_TOKEN must be configured')

        qs = AIContent.objects.all().order_by('created_at', 'id')
        if options['ids']:
            qs = qs.filter(id__in=options['ids'])
        if options['limit'] and options['limit'] > 0:
            qs = qs[:options['limit']]

        client = None if options['dry_run'] else CorunClient()
        stats = {
            'seen': 0,
            'created': 0,
            'already_mapped': 0,
            'linked': 0,
            'unresolved_subject': 0,
            'errors': 0,
        }

        for row in qs:
            stats['seen'] += 1
            try:
                result = self._process_row(row, client=client, dry_run=options['dry_run'])
            except CorunAPIError as exc:
                stats['errors'] += 1
                self.stderr.write(self.style.ERROR(f'AIContent {row.pk}: {exc}'))
                continue
            stats[result] += 1

        self.stdout.write(
            self.style.SUCCESS(
                'AIContent corun-ai backfill: '
                + ', '.join(f'{key}={value}' for key, value in stats.items())
            )
        )

    def _process_row(self, row, *, client, dry_run):
        data = row.data if isinstance(row.data, dict) else {}
        page_group_id = str(data.get(CORUN_MAPPING_KEY) or '').strip()
        if page_group_id:
            linked = self._link_subject(row, page_group_id, dry_run=dry_run)
            self.stdout.write(
                f'AIContent {row.pk}: already mapped to corun-ai Page {page_group_id}'
                + ('; linked subject pointer' if linked else '')
            )
            return 'linked' if linked else 'already_mapped'

        if dry_run:
            self.stdout.write(
                f'AIContent {row.pk}: would create corun-ai Page for '
                f'{row.subject_type}:{row.subject_key}'
            )
            return 'created'

        page = client.create_page(
            section=CORUN_ASSESSMENT_SECTION,
            title=f'AI assessment: {row.subject_type} {row.subject_key}',
            content=row.assessment,
            data=self._page_data(row, data),
            tags=[
                'epicprod',
                'ai-assessment',
                str(row.subject_type or '').replace('_', '-'),
                'legacy-ai-content',
            ],
        )
        page_group_id = str(page.get('group_id') or '')
        if not page_group_id:
            raise CorunAPIError(f'corun-ai Page response for AIContent {row.pk} had no group_id')

        updated_data = dict(data)
        updated_data[CORUN_MAPPING_KEY] = page_group_id
        updated_data['corun_page_id'] = str(page.get('id') or '')
        updated_data['corun_backfilled_at'] = timezone.now().isoformat()
        row.data = updated_data
        row.save(update_fields=['data'])

        linked = self._link_subject(row, page_group_id, dry_run=False)
        self.stdout.write(
            f'AIContent {row.pk}: created corun-ai Page {page_group_id}'
            + (' and linked subject pointer' if linked else '; subject not resolved')
        )
        return 'created' if linked else 'unresolved_subject'

    def _page_data(self, row, data):
        payload = dict(data)
        payload.update({
            'artifact_type': 'ai_assessment',
            'source_system': 'swf-monitor',
            'ui_visible': False,
            'subject_type': row.subject_type,
            'subject_key': row.subject_key,
            'subject_label': row.subject_label,
            'subject_url': row.subject_url,
            'created_by_system': 'epicprod',
            'created_by_user': row.username,
            'ai': row.ai,
            'legacy_ai_content_id': row.pk,
            'legacy_created_at': row.created_at.isoformat() if row.created_at else '',
        })
        payload.setdefault(AI_CONTENT_QUALITY_KEY, data.get(AI_CONTENT_QUALITY_KEY) or '')
        payload.setdefault(AI_CONTENT_COMMENT_KEY, data.get(AI_CONTENT_COMMENT_KEY) or '')
        return payload

    def _link_subject(self, row, page_group_id, *, dry_run):
        resolved = self._resolve_subject(row)
        if not resolved:
            return False
        target_obj, json_field = resolved
        current = getattr(target_obj, json_field) or {}
        if page_group_id in corun_page_group_ids(current):
            return False
        if not dry_run:
            append_corun_page_group_id(target_obj, json_field, page_group_id)
        return True

    def _resolve_subject(self, row):
        subject_type = str(row.subject_type or '').strip()
        subject_key = str(row.subject_key or '').strip()
        if not subject_type or not subject_key:
            return None

        if subject_type == 'campaign_task':
            return self._resolve_campaign_task(subject_key)
        if subject_type == 'panda_task':
            return self._resolve_panda_task(subject_key)
        if subject_type == 'panda_job':
            return self._resolve_panda_job(subject_key)
        if subject_type == 'panda_queue':
            queue = PandaQueue.objects.filter(queue_name=subject_key).first()
            return (queue, 'metadata') if queue else None
        return None

    def _resolve_campaign_task(self, subject_key):
        from pcs.models import ProdTask

        qs = ProdTask.objects.select_related('dataset')
        task = qs.filter(name=subject_key).first()
        if not task:
            for candidate in qs.all():
                if candidate.composed_name == subject_key:
                    task = candidate
                    break
        return (task, 'overrides') if task else None

    def _resolve_panda_task(self, subject_key):
        from pcs.models import PandaTasks

        qs = PandaTasks.objects.all()
        if subject_key.isdigit():
            row = qs.filter(jedi_task_id=int(subject_key)).first()
        else:
            row = qs.filter(task_name=subject_key).first()
        return (row, 'metadata') if row else None

    def _resolve_panda_job(self, subject_key):
        try:
            pandaid = int(subject_key)
        except ValueError:
            return None
        row = EpicProdJob.objects.filter(pandaid=pandaid).first()
        return (row, 'data') if row else None
