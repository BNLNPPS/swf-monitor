from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('pcs', '0019_restore_detector_version_composed_name'),
        ('monitor_app', '0035_add_tf_counts'),
    ]

    operations = [
        migrations.CreateModel(
            name='EpicProdJob',
            fields=[
                ('pandaid', models.BigIntegerField(primary_key=True, serialize=False)),
                ('jeditaskid', models.BigIntegerField(blank=True, db_index=True, null=True)),
                ('seq_number', models.IntegerField(blank=True, db_index=True, null=True)),
                ('job_index', models.IntegerField(blank=True, db_index=True, null=True)),
                ('status', models.CharField(blank=True, db_index=True, default='', max_length=40)),
                ('phase', models.CharField(blank=True, db_index=True, default='', max_length=80)),
                ('failure_summary', models.TextField(blank=True, default='')),
                ('data', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('last_refreshed_at', models.DateTimeField(blank=True, null=True)),
                ('prod_task', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='epicprod_jobs', to='pcs.prodtask')),
            ],
            options={
                'db_table': 'swf_epicprod_jobs',
                'ordering': ['-pandaid'],
            },
        ),
        migrations.CreateModel(
            name='EpicProdFile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('jeditaskid', models.BigIntegerField(blank=True, db_index=True, null=True)),
                ('pandaid', models.BigIntegerField(blank=True, db_index=True, null=True)),
                ('seq_number', models.IntegerField(blank=True, db_index=True, null=True)),
                ('job_index', models.IntegerField(blank=True, db_index=True, null=True)),
                ('role', models.CharField(choices=[('input', 'Input'), ('output', 'Output'), ('log', 'Log')], db_index=True, max_length=20)),
                ('stage', models.CharField(blank=True, db_index=True, default='', max_length=40)),
                ('scope', models.CharField(blank=True, db_index=True, default='', max_length=100)),
                ('dataset_name', models.CharField(blank=True, default='', max_length=1024)),
                ('did_name', models.CharField(blank=True, db_index=True, default='', max_length=1024)),
                ('lfn', models.CharField(blank=True, default='', max_length=512)),
                ('rse_expected', models.CharField(blank=True, default='', max_length=100)),
                ('status', models.CharField(choices=[('expected', 'Expected'), ('produced_local', 'Produced locally'), ('validated', 'Validated'), ('registered', 'Registered'), ('failed', 'Failed'), ('conflict', 'Conflict'), ('missing', 'Missing'), ('unknown', 'Unknown')], db_index=True, default='expected', max_length=40)),
                ('status_detail', models.TextField(blank=True, default='')),
                ('bytes', models.BigIntegerField(blank=True, null=True)),
                ('checksum', models.CharField(blank=True, default='', max_length=128)),
                ('source', models.CharField(blank=True, db_index=True, default='', max_length=80)),
                ('data', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('job', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='files', to='monitor_app.epicprodjob')),
                ('prod_task', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='epicprod_files', to='pcs.prodtask')),
            ],
            options={
                'db_table': 'swf_epicprod_files',
                'ordering': ['jeditaskid', 'seq_number', 'role', 'stage', 'lfn'],
            },
        ),
        migrations.AddIndex(
            model_name='epicprodjob',
            index=models.Index(fields=['jeditaskid', 'seq_number'], name='swf_epicpro_jeditas_b940cb_idx'),
        ),
        migrations.AddIndex(
            model_name='epicprodjob',
            index=models.Index(fields=['prod_task', 'status'], name='swf_epicpro_prod_ta_95dd46_idx'),
        ),
        migrations.AddIndex(
            model_name='epicprodjob',
            index=models.Index(fields=['phase', 'status'], name='swf_epicpro_phase_340edd_idx'),
        ),
        migrations.AddIndex(
            model_name='epicprodfile',
            index=models.Index(fields=['prod_task', 'role', 'stage'], name='swf_epicpro_prod_ta_d2532e_idx'),
        ),
        migrations.AddIndex(
            model_name='epicprodfile',
            index=models.Index(fields=['jeditaskid', 'seq_number'], name='swf_epicpro_jeditas_59e5b7_idx'),
        ),
        migrations.AddIndex(
            model_name='epicprodfile',
            index=models.Index(fields=['pandaid', 'role', 'stage'], name='swf_epicpro_pandaid_5e6523_idx'),
        ),
        migrations.AddIndex(
            model_name='epicprodfile',
            index=models.Index(fields=['scope', 'did_name'], name='swf_epicpro_scope_a6bd57_idx'),
        ),
        migrations.AddIndex(
            model_name='epicprodfile',
            index=models.Index(fields=['status', 'stage'], name='swf_epicpro_status_f4300f_idx'),
        ),
    ]
