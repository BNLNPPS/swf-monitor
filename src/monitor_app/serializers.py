from rest_framework import serializers
from .models import SystemAgent

class SystemAgentSerializer(serializers.ModelSerializer):
    class Meta:
        model = SystemAgent
        fields = ['id', 'instance_name', 'agent_type', 'description', 'status', 'last_heartbeat', 'agent_url', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']
