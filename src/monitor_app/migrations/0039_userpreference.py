from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('monitor_app', '0038_alarm_entries'),
    ]

    operations = [
        migrations.CreateModel(
            name='UserPreference',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('username', models.CharField(max_length=150, unique=True)),
                ('prefs', models.JSONField(blank=True, default=dict)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'user_preference',
                'ordering': ['username'],
            },
        ),
    ]
