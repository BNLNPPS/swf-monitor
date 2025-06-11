from django.db import models

class MonitoredItem(models.Model):
    STATUS_CHOICES = [
        ('UNKNOWN', 'Unknown'),
        ('OK', 'OK'),
        ('WARNING', 'Warning'),
        ('ERROR', 'Error'),
    ]

    name = models.CharField(max_length=100, unique=True)  # Added unique=True
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

    def __str__(self):
        return self.name
