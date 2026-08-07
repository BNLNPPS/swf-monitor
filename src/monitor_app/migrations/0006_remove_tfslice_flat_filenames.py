from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('monitor_app', '0005_tfslice_fastmon_file'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='tfslice',
            name='stf_filename',
        ),
        migrations.RemoveField(
            model_name='tfslice',
            name='tf_filename',
        ),
    ]
