# Generated manually for renaming file_url to stf_filename

from django.db import migrations, models


def populate_stf_filename(apps, schema_editor):
    """
    Populate stf_filename field with dummy data for existing records.
    The file_url field contained garbage data, so we'll use dummy filenames.
    """
    StfFile = apps.get_model('monitor_app', 'StfFile')
    
    for i, stf_file in enumerate(StfFile.objects.all()):
        # Create dummy filename based on file_id and index
        dummy_filename = f"migrated_stf_{i+1}_{str(stf_file.file_id)[:8]}.stf"
        stf_file.stf_filename = dummy_filename
        stf_file.save()


def reverse_populate_stf_filename(apps, schema_editor):
    """
    Reverse migration - populate file_url with dummy data.
    """
    StfFile = apps.get_model('monitor_app', 'StfFile')
    
    for stf_file in StfFile.objects.all():
        # Create dummy URL
        dummy_url = f"file:///dummy/path/{stf_file.stf_filename}"
        stf_file.file_url = dummy_url
        stf_file.save()


class Migration(migrations.Migration):

    dependencies = [
        ('monitor_app', '0015_persistentstate_and_more'),
    ]

    operations = [
        # Step 1: Add the new stf_filename field (nullable initially)
        migrations.AddField(
            model_name='stffile',
            name='stf_filename',
            field=models.CharField(max_length=255, null=True),
        ),
        
        # Step 2: Populate the new field with dummy data
        migrations.RunPython(
            populate_stf_filename,
            reverse_populate_stf_filename,
        ),
        
        # Step 3: Make stf_filename non-nullable and unique
        migrations.AlterField(
            model_name='stffile',
            name='stf_filename',
            field=models.CharField(max_length=255, unique=True),
        ),
        
        # Step 4: Remove the old file_url field
        migrations.RemoveField(
            model_name='stffile',
            name='file_url',
        ),
    ]