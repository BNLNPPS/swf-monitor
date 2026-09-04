# Seed the pings alarm entry (docs/PINGS.md): the one alarm configuration
# behind every ping, carried by the engine's `pings` detect module.
# Recipients are the ping subscribers, edited on the alarm dashboard.
import time

from django.db import migrations


def seed(apps, schema_editor):
    EntryContext = apps.get_model('monitor_app', 'EntryContext')
    Entry = apps.get_model('monitor_app', 'Entry')
    ctx, _ = EntryContext.objects.get_or_create(
        name='swf-alarms', defaults={'title': 'swf-alarms'})
    if Entry.objects.filter(context=ctx, kind='alarm',
                            data__entry_id='alarm_pings').exists():
        return
    now = time.time()
    Entry.objects.create(
        title='Pings', kind='alarm', context=ctx, status='active',
        content=('A ping is a dated obligation: an action to take on a '
                 'subject by a due date. This alarm raises each open ping '
                 'once its due date is within its lead time, repeats it '
                 'weekly, and raises it as an alarm once the due date has '
                 'passed. Mark the ping fulfilled on the alarm dashboard '
                 'when the obligation is met; its event clears on the '
                 'next tick.'),
        data={
            'entry_id': 'alarm_pings',
            'enabled': True,
            'severity': 'ping',
            'recipients': '@prodops',
            'renotification_window_hours': 168,
            'params': {'default_lead_days': 7},
        },
        timestamp_created=now, timestamp_modified=now)


def unseed(apps, schema_editor):
    Entry = apps.get_model('monitor_app', 'Entry')
    Entry.objects.filter(context_id='swf-alarms', kind='alarm',
                         data__entry_id='alarm_pings').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('monitor_app', '0011_seed_bind_mount_rule'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
