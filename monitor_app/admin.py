from django.contrib import admin
from .models import MonitoredItem

@admin.register(MonitoredItem)
class MonitoredItemAdmin(admin.ModelAdmin):
    list_display = ('name', 'status', 'last_heartbeat', 'agent_url')
    list_filter = ('status',)
    search_fields = ('name', 'agent_url')
