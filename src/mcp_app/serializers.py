from rest_framework import serializers
from datetime import datetime


class MCPRequestSerializer(serializers.Serializer):
    """Base serializer for MCP requests with common fields."""
    mcp_version = serializers.CharField(max_length=10, default="1.0")
    message_id = serializers.CharField(max_length=100)
    
    def validate_mcp_version(self, value):
        if value != "1.0":
            raise serializers.ValidationError("Unsupported MCP version. Server supports 1.0.")
        return value


class MCPResponseSerializer(serializers.Serializer):
    """Base serializer for MCP responses with common fields."""
    mcp_version = serializers.CharField(default="1.0")
    message_id = serializers.CharField()
    in_reply_to = serializers.CharField()
    status = serializers.CharField()
    payload = serializers.JSONField(required=False)


class MCPErrorResponseSerializer(serializers.Serializer):
    """Serializer for MCP error responses."""
    mcp_version = serializers.CharField(default="1.0")
    message_id = serializers.CharField()
    in_reply_to = serializers.CharField(required=False)
    status = serializers.CharField(default="error")
    error = serializers.JSONField()


class HeartbeatPayloadSerializer(serializers.Serializer):
    """Serializer for heartbeat payload data."""
    name = serializers.CharField(max_length=255)
    timestamp = serializers.CharField()
    status = serializers.ChoiceField(choices=[
        ('UNKNOWN', 'Unknown'),
        ('OK', 'OK'),
        ('WARNING', 'Warning'),
        ('ERROR', 'Error'),
    ])
    
    def validate_timestamp(self, value):
        try:
            datetime.fromisoformat(value)
        except ValueError:
            raise serializers.ValidationError("Invalid timestamp format. Use ISO format.")
        return value


class HeartbeatRequestSerializer(MCPRequestSerializer):
    """Serializer for heartbeat requests."""
    command = serializers.CharField(default="heartbeat")
    payload = HeartbeatPayloadSerializer()


class DiscoverCapabilitiesRequestSerializer(MCPRequestSerializer):
    """Serializer for discover capabilities requests."""
    command = serializers.CharField(default="discover_capabilities")
    payload = serializers.JSONField(default=dict)


class GetAgentLivenessRequestSerializer(MCPRequestSerializer):
    """Serializer for get agent liveness requests."""
    command = serializers.CharField(default="get_agent_liveness")
    payload = serializers.JSONField(default=dict)


class CapabilitiesResponseSerializer(serializers.Serializer):
    """Serializer for capabilities response payload."""
    discover_capabilities = serializers.CharField()
    get_agent_liveness = serializers.CharField()
    heartbeat = serializers.CharField()


class AgentLivenessResponseSerializer(serializers.Serializer):
    """Serializer for agent liveness response payload."""
    # Dynamic field for agent statuses (agent_name: 'alive'|'dead')
    def to_representation(self, instance):
        # instance should be a dict of {agent_name: status}
        return instance