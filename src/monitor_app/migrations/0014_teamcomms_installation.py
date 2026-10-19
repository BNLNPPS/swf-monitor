"""Provision the SWF team using the host's existing identity system."""

from django.db import migrations

TEAM_ID = "8873a572-a319-4a90-bfc9-76a9b76e30fb"


def provision_team(apps, schema_editor):
    Team = apps.get_model("teamcomms_service", "Team")
    Team.objects.using(schema_editor.connection.alias).get_or_create(
        id=TEAM_ID, defaults={"name": "ePIC Workflow Management"})


class Migration(migrations.Migration):
    dependencies = [
        ("monitor_app", "0013_crashsignature"),
        ("teamcomms_service", "0002_host_identity_binding"),
        ("teamcomms_entries", "0002_integrity_and_search"),
        ("teamcomms_comms", "0002_immutable_records"),
    ]
    operations = [migrations.RunPython(provision_team, migrations.RunPython.noop)]
