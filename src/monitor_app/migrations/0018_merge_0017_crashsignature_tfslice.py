from django.db import migrations


class Migration(migrations.Migration):
    """Joins the two 0017 migrations, made in parallel on the v46 baseline
    and in swf-monitor PR #53. It changes no schema."""

    dependencies = [
        ('monitor_app', '0017_crashsignature_memory_class'),
        ('monitor_app', '0017_tfslice_result_error'),
    ]

    operations = []
