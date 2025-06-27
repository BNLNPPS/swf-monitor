from django.db import migrations

class Migration(migrations.Migration):
    dependencies = [
        ("monitor_app", "0004_applog"),
    ]

    operations = [
        migrations.AlterModelTable(
            name="AppLog",
            table="swf_applog",
        ),
        migrations.AlterModelTable(
            name="SystemAgent",
            table="swf_systemagent",
        ),
    ]
