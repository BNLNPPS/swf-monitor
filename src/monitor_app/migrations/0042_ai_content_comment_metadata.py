# Generated manually on 2026-06-29

from django.db import migrations


def add_comment_metadata(apps, schema_editor):
    AIContent = apps.get_model('monitor_app', 'AIContent')
    for row in AIContent.objects.all().iterator():
        data = row.data or {}
        if not isinstance(data, dict):
            data = {}
        else:
            data = dict(data)
        changed = False
        if 'quality' not in data:
            data['quality'] = ''
            changed = True
        if 'comment' not in data:
            data['comment'] = ''
            changed = True
        if changed:
            row.data = data
            row.save(update_fields=['data'])


class Migration(migrations.Migration):

    dependencies = [
        ('monitor_app', '0041_ai_content_quality_metadata'),
    ]

    operations = [
        migrations.RunPython(add_comment_metadata, migrations.RunPython.noop),
    ]
