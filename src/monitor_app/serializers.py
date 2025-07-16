from rest_framework import serializers
from .models import SystemAgent, AppLog
from .workflow_models import STFWorkflow, AgentWorkflowStage, WorkflowMessage

class SystemAgentSerializer(serializers.ModelSerializer):
    class Meta:
        model = SystemAgent
        fields = ['id', 'instance_name', 'agent_type', 'description', 'status', 'last_heartbeat', 'agent_url', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

class AppLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AppLog
        fields = '__all__'

class STFWorkflowSerializer(serializers.ModelSerializer):
    class Meta:
        model = STFWorkflow
        fields = '__all__'

class AgentWorkflowStageSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentWorkflowStage
        fields = '__all__'

class WorkflowMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowMessage
        fields = '__all__'

class LogSummarySerializer(serializers.Serializer):
    error_counts = serializers.DictField(child=serializers.IntegerField())
    recent_errors = serializers.ListField(child=serializers.DictField())
