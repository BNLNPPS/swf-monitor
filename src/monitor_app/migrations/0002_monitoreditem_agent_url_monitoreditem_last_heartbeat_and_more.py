# Generated by Django 4.2.23 on 2025-06-11 16:52

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("monitor_app", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="monitoreditem",
            name="agent_url",
            field=models.URLField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="monitoreditem",
            name="last_heartbeat",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="monitoreditem",
            name="status",
            field=models.CharField(
                choices=[
                    ("UNKNOWN", "Unknown"),
                    ("OK", "OK"),
                    ("WARNING", "Warning"),
                    ("ERROR", "Error"),
                ],
                default="UNKNOWN",
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="monitoreditem",
            name="name",
            field=models.CharField(max_length=100, unique=True),
        ),
    ]
