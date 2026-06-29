# Generated manually on 2026-06-29

from django.db import migrations


def add_quality_metadata(apps, schema_editor):
    AIContent = apps.get_model('monitor_app', 'AIContent')
    for row in AIContent.objects.all().iterator():
        data = row.data or {}
        if not isinstance(data, dict):
            data = {}
        else:
            data = dict(data)
        if 'quality' in data:
            continue
        data['quality'] = ''
        row.data = data
        row.save(update_fields=['data'])


class Migration(migrations.Migration):

    dependencies = [
        ('monitor_app', '0040_ai_content'),
    ]

    operations = [
        migrations.RunPython(add_quality_metadata, migrations.RunPython.noop),
    ]
