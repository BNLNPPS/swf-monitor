from rest_framework import serializers
from .models import MonitoredItem

class MonitoredItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = MonitoredItem
        fields = ['id', 'name', 'description', 'status', 'last_heartbeat', 'agent_url', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']  # Allow status and last_heartbeat to be updated
