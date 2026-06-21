import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from monitor_app.models import Entry, EntryContext, EntryVersion


ALARM_CONTEXTS = {'swf-alarms', 'teams'}


class Command(BaseCommand):
    help = 'Import alarm Entry/EntryContext/EntryVersion state exported from swf-remote.'

    def add_arguments(self, parser):
        parser.add_argument('path', help='Path to swf-alarms-export.json')
        parser.add_argument(
            '--replace',
            action='store_true',
            help='Replace existing monitor swf-alarms and teams contexts.',
        )

    def handle(self, *args, **options):
        path = options['path']
        try:
            with open(path) as f:
                payload = json.load(f)
        except OSError as e:
            raise CommandError(f'Cannot read {path}: {e}') from e
        except json.JSONDecodeError as e:
            raise CommandError(f'Invalid JSON in {path}: {e}') from e

        contexts = payload.get('contexts') or []
        entries = payload.get('entries') or []
        versions = payload.get('versions') or []

        context_names = {c.get('name') for c in contexts}
        if not context_names <= ALARM_CONTEXTS:
            raise CommandError(
                f'Unexpected context(s): {sorted(context_names - ALARM_CONTEXTS)}'
            )
        for row in entries:
            if row.get('context_id') not in ALARM_CONTEXTS:
                raise CommandError(f"Unexpected entry context: {row.get('context_id')}")

        existing = Entry.objects.filter(context_id__in=ALARM_CONTEXTS).exists()
        if existing and not options['replace']:
            raise CommandError(
                'Alarm/team entries already exist. Re-run with --replace to '
                'replace monitor alarm state.'
            )

        with transaction.atomic():
            if options['replace']:
                EntryVersion.objects.filter(entry__context_id__in=ALARM_CONTEXTS).delete()
                Entry.objects.filter(context_id__in=ALARM_CONTEXTS).delete()
                EntryContext.objects.filter(name__in=ALARM_CONTEXTS).delete()

            for row in contexts:
                EntryContext.objects.create(
                    name=row['name'],
                    title=row.get('title') or '',
                    description=row.get('description') or '',
                    timestamp_created=row.get('timestamp_created') or 0,
                    timestamp_modified=row.get('timestamp_modified') or 0,
                    data=row.get('data') or {},
                )

            pending_parents = []
            for row in entries:
                pending_parents.append((row['id'], row.get('parent_id')))
                Entry.objects.create(
                    id=row['id'],
                    title=row.get('title') or '',
                    content=row.get('content') or '',
                    kind=row['kind'],
                    context_id=row.get('context_id'),
                    name=row.get('name'),
                    data=row.get('data'),
                    priority=row.get('priority'),
                    status=row.get('status'),
                    archived=bool(row.get('archived')),
                    parent_id=None,
                    timestamp_created=row.get('timestamp_created') or 0,
                    timestamp_modified=row.get('timestamp_modified') or 0,
                    deleted_at=row.get('deleted_at'),
                )

            for entry_id, parent_id in pending_parents:
                if parent_id:
                    Entry.objects.filter(id=entry_id).update(parent_id=parent_id)

            for row in versions:
                EntryVersion.objects.create(
                    entry_id=row['entry_id'],
                    version_num=row['version_num'],
                    title=row.get('title') or '',
                    content=row.get('content') or '',
                    data=row.get('data'),
                    changed_by=row.get('changed_by') or 'unknown',
                    timestamp=row.get('timestamp') or 0,
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(contexts)} contexts, {len(entries)} entries, "
                f"and {len(versions)} versions."
            )
        )
