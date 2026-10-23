from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('monitor_app', '0016_pandataskoperation_reassign'),
    ]

    operations = [
        migrations.AddField(
            model_name='tfslice',
            name='result',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tfslice',
            name='error',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
