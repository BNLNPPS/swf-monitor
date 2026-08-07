import django.db.models.deletion
from django.db import migrations, models


def populate_fastmon_file(apps, schema_editor):
    TFSlice = apps.get_model('monitor_app', 'TFSlice')
    FastMonFile = apps.get_model('monitor_app', 'FastMonFile')

    fastmon_id_by_tf_filename = dict(
        FastMonFile.objects.values_list('tf_filename', 'tf_file_id')
    )

    unmatched = []
    for slice_obj in TFSlice.objects.all():
        fastmon_id = fastmon_id_by_tf_filename.get(slice_obj.tf_filename)
        if fastmon_id is None:
            unmatched.append(slice_obj.pk)
            continue
        slice_obj.fastmon_file_id = fastmon_id
        slice_obj.save(update_fields=['fastmon_file'])

    if unmatched:
        raise RuntimeError(
            f"Cannot migrate {len(unmatched)} TFSlice row(s) with no matching "
            f"FastMonFile by tf_filename (ids: "
            f"{unmatched[:20]}{'...' if len(unmatched) > 20 else ''})"
        )


def restore_flat_filenames(apps, schema_editor):
    TFSlice = apps.get_model('monitor_app', 'TFSlice')
    for slice_obj in TFSlice.objects.select_related('fastmon_file', 'fastmon_file__stf_file').all():
        slice_obj.tf_filename = slice_obj.fastmon_file.tf_filename
        slice_obj.stf_filename = slice_obj.fastmon_file.stf_file.stf_filename
        slice_obj.save(update_fields=['tf_filename', 'stf_filename'])


class Migration(migrations.Migration):

    dependencies = [
        ('monitor_app', '0002_cachedproduct'),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name='tfslice',
            name='swf_tf_slic_stf_fil_45fc7b_idx',
        ),
        migrations.AlterUniqueTogether(
            name='tfslice',
            unique_together=set(),
        ),
        migrations.AddField(
            model_name='tfslice',
            name='fastmon_file',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='tf_slices', to='monitor_app.fastmonfile'),
        ),
        migrations.RunPython(populate_fastmon_file, restore_flat_filenames),
        migrations.AlterField(
            model_name='tfslice',
            name='fastmon_file',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='tf_slices', to='monitor_app.fastmonfile'),
        ),
        migrations.AddIndex(
            model_name='tfslice',
            index=models.Index(fields=['fastmon_file', 'status'], name='swf_tf_slic_fastmon_status_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='tfslice',
            unique_together={('fastmon_file', 'slice_id')},
        ),
    ]
