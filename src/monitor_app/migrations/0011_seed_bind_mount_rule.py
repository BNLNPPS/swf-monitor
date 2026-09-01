# Seed the first label-reliability rule of the error-correction root
# (docs/ERROR_ATTRIBUTION.md): the pilot 1305 "bind mounting" text is
# apptainer teardown noise the pilot picks up when the payload dies —
# never the actual failure. Confirmed against payload logs (jobs
# 2584600, 2690539, 2704064); the corrected reading refines from the
# matched jobs' payload exit codes.
from django.db import migrations


def seed(apps, schema_editor):
    ErrorCorrectionRule = apps.get_model('monitor_app',
                                         'ErrorCorrectionRule')
    ErrorCorrectionRule.objects.get_or_create(
        component='pilot', code=1305,
        diag_substring='While bind mounting',
        defaults={
            'corrected_label': 'payload failure (mode undetermined); '
                               'the bind-mount text is container '
                               'teardown noise, not the cause',
            'note': 'The pilot reports stale apptainer stderr as the '
                    'payload failure. The real failure is in the '
                    'payload log; the exit-code profile gives the '
                    'mode. See docs/ERROR_ATTRIBUTION.md.',
            'evidence_url': 'https://github.com/BNLNPPS/swf-monitor/'
                            'blob/infra/baseline-v43/docs/'
                            'ERROR_ATTRIBUTION.md',
            'created_by': 'error-attribution',
        })


def unseed(apps, schema_editor):
    ErrorCorrectionRule = apps.get_model('monitor_app',
                                         'ErrorCorrectionRule')
    ErrorCorrectionRule.objects.filter(
        component='pilot', code=1305,
        diag_substring='While bind mounting').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('monitor_app', '0010_errorcorrectionrule'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
