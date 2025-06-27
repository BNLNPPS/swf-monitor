import logging
from django.db import models

class SystemAgent(models.Model):
    STATUS_CHOICES = [
        ('UNKNOWN', 'Unknown'),
        ('OK', 'OK'),
        ('WARNING', 'Warning'),
        ('ERROR', 'Error'),
    ]

    instance_name = models.CharField(max_length=100, unique=True)
    agent_type = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default='UNKNOWN',
    )
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    agent_url = models.URLField(max_length=200, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'swf_systemagent'

    def __str__(self):
        return self.instance_name

class AppLog(models.Model):
    LEVEL_CHOICES = [
        (logging.CRITICAL, 'CRITICAL'),
        (logging.ERROR, 'ERROR'),
        (logging.WARNING, 'WARNING'),
        (logging.INFO, 'INFO'),
        (logging.DEBUG, 'DEBUG'),
        (logging.NOTSET, 'NOTSET'),
    ]
    app_name = models.CharField(max_length=100, db_index=True)
    instance_name = models.CharField(max_length=100, db_index=True)
    timestamp = models.DateTimeField(db_index=True)
    level = models.IntegerField(choices=LEVEL_CHOICES, default=logging.NOTSET, db_index=True)
    level_name = models.CharField(max_length=50)
    message = models.TextField()
    module = models.CharField(max_length=255)
    func_name = models.CharField(max_length=255)
    line_no = models.IntegerField()
    process = models.IntegerField()
    thread = models.BigIntegerField()
    extra_data = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = 'swf_applog'
        ordering = ['-timestamp']
        verbose_name_plural = "App Logs"
        indexes = [
            models.Index(fields=['-timestamp', 'app_name', 'instance_name']),
        ]

    def __str__(self):
        return f'{self.timestamp} - {self.app_name}:{self.instance_name} - {self.get_level_display()} - {self.message}'
