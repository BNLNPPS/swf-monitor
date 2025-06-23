from django.contrib import admin
from .models import SystemAgent

@admin.register(SystemAgent)
class SystemAgentAdmin(admin.ModelAdmin):
    list_display = ('instance_name', 'agent_type', 'status', 'last_heartbeat', 'agent_url')
    list_filter = ('status', 'agent_type')
    search_fields = ('instance_name', 'agent_type', 'agent_url')
