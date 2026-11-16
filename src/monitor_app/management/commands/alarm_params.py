"""Show or set an alarm's stored parameters.

The alarm engine passes each alarm the parameters stored on its row
(``data.params``); a module falls back to its own ``PARAMS`` defaults only
for keys the row lacks. This is the command-line counterpart of the alarm
editor's save (``alarm_views.alarm_config_save``): the same row, the same
version record, attributed to ``--by``. Keys named in ``--set`` are changed
and every other stored key is kept.

    python src/manage.py alarm_params alarm_tls_cert_expiry
    python src/manage.py alarm_params alarm_tls_cert_expiry \\
        --set "hosts=a.example.org, b.example.org:8443" --by swf-1

A value that parses as JSON (a number, true, a list) is stored as parsed;
anything else is stored as the string given.
"""
import json
import time

from django.core.management.base import BaseCommand, CommandError

from monitor_app import alarms_data
from monitor_app.models import EntryVersion
from monitor_app.signals import set_changed_by


def _parse(value):
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


class Command(BaseCommand):
    help = "Show or set an alarm's stored parameters (data.params)."

    def add_arguments(self, parser):
        parser.add_argument('entry_id', help='the alarm row, e.g. alarm_tls_cert_expiry')
        parser.add_argument('--set', action='append', default=[], metavar='KEY=VALUE',
                            help='set one parameter; repeat for several')
        parser.add_argument('--by', default='', help='who makes the change (required with --set)')

    def handle(self, *args, **options):
        alarm = alarms_data.get_alarm_config_by_entry_id(options['entry_id'])
        if alarm is None:
            raise CommandError(f"No alarm row {options['entry_id']!r}")
        data = dict(alarm.data or {})
        params = dict(data.get('params') or {})

        if options['set']:
            if not options['by']:
                raise CommandError('--by is required with --set')
            for item in options['set']:
                key, sep, value = item.partition('=')
                if not sep or not key.strip():
                    raise CommandError(f'Not KEY=VALUE: {item!r}')
                params[key.strip()] = _parse(value)
            data['params'] = params
            set_changed_by(f"cli:{options['by']}")
            alarm.data = data
            alarm.timestamp_modified = time.time()
            alarm.save()
            latest = (EntryVersion.objects.filter(entry=alarm)
                      .order_by('-version_num').values('version_num').first())
            self.stdout.write(f"saved, version {latest['version_num'] if latest else 0}")

        self.stdout.write(json.dumps(params, indent=2, sort_keys=True))
