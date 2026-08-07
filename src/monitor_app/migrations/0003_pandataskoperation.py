# Generated for durable PanDA pause/resume operations.

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('monitor_app', '0002_cachedproduct'),
    ]

    operations = [
        migrations.CreateModel(
            name='PandaTaskOperation',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('jedi_task_id', models.BigIntegerField(db_index=True)),
                ('task_name', models.CharField(blank=True, default='', max_length=500)),
                ('operation', models.CharField(choices=[('pause', 'Pause'), ('resume', 'Resume')], max_length=20)),
                ('source', models.CharField(default='manual', max_length=40)),
                ('requested_by', models.CharField(blank=True, default='', max_length=100)),
                ('status', models.CharField(choices=[('queued', 'Queued'), ('running', 'Running'), ('accepted', 'Accepted by PanDA'), ('verified', 'Verified'), ('failed', 'Failed'), ('timeout', 'Timed out'), ('unverified', 'Accepted but unverified')], db_index=True, default='queued', max_length=20)),
                ('diagnostic', models.TextField(blank=True, default='')),
                ('observed_status', models.CharField(blank=True, default='', max_length=50)),
                ('evidence', models.JSONField(blank=True, default=dict)),
                ('requested_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('accepted_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'swf_panda_task_operation',
                'ordering': ['-requested_at'],
                'indexes': [models.Index(fields=['jedi_task_id', 'requested_at'], name='swf_panda_t_jedi_ta_709a65_idx'), models.Index(fields=['status', 'requested_at'], name='swf_panda_t_status_39c5b0_idx')],
                'constraints': [models.UniqueConstraint(condition=models.Q(status__in=('queued', 'running', 'accepted')), fields=('jedi_task_id',), name='uniq_pending_panda_task_operation')],
            },
        ),
    ]
