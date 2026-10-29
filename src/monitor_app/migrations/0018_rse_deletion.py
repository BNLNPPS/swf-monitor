from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('monitor_app', '0017_crashsignature_memory_class'),
    ]

    operations = [
        migrations.CreateModel(
            name='RseDeletion',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('rse', models.CharField(db_index=True, max_length=100)),
                ('pattern', models.CharField(max_length=1000)),
                ('requested_by', models.CharField(blank=True, default='', max_length=100)),
                ('confirmed_by', models.CharField(blank=True, default='', max_length=100)),
                ('status', models.CharField(choices=[('previewing', 'Previewing'), ('previewed', 'Previewed'), ('queued', 'Queued for removal'), ('running', 'Removing'), ('done', 'Done'), ('partial', 'Done with failures'), ('failed', 'Failed')], db_index=True, default='previewing', max_length=20)),
                ('diagnostic', models.TextField(blank=True, default='')),
                ('plan', models.JSONField(blank=True, default=dict)),
                ('result', models.JSONField(blank=True, default=dict)),
                ('requested_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('previewed_at', models.DateTimeField(blank=True, null=True)),
                ('confirmed_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'swf_rse_deletion',
                'ordering': ['-requested_at'],
                'constraints': [models.UniqueConstraint(condition=models.Q(('status__in', ('queued', 'running'))), fields=('rse',), name='uniq_executing_rse_deletion')],
            },
        ),
    ]
