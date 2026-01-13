from rest_framework import serializers
from .models import SystemAgent, AppLog, Run, StfFile, Subscriber, FastMonFile, TFSlice, Worker, RunState, SystemStateEvent
from .workflow_models import STFWorkflow, AgentWorkflowStage, WorkflowMessage, WorkflowDefinition, WorkflowExecution

class SystemAgentSerializer(serializers.ModelSerializer):
    class Meta:
        model = SystemAgent
        fields = [
            'id', 'instance_name', 'agent_type', 'description', 'status',
            'last_heartbeat', 'agent_url', 'workflow_enabled',
            'current_stf_count', 'total_stf_processed', 'last_stf_processed',
            'pid', 'hostname', 'operational_state', 'namespace',
            'metadata', 'created_at', 'updated_at'
        ]
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

class RunSerializer(serializers.ModelSerializer):
    class Meta:
        model = Run
        fields = '__all__'

class StfFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = StfFile
        fields = '__all__'

class SubscriberSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subscriber
        fields = '__all__'

class FastMonFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = FastMonFile
        fields = '__all__'


class WorkflowDefinitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowDefinition
        fields = '__all__'


class WorkflowExecutionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowExecution
        fields = '__all__'

class LogSummarySerializer(serializers.Serializer):
    error_counts = serializers.DictField(child=serializers.IntegerField())
    recent_errors = serializers.ListField(child=serializers.DictField())


# Fast Processing models serializers

class TFSliceSerializer(serializers.ModelSerializer):
    class Meta:
        model = TFSlice
        fields = '__all__'


class WorkerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Worker
        fields = '__all__'


class RunStateSerializer(serializers.ModelSerializer):
    class Meta:
        model = RunState
        fields = '__all__'


class SystemStateEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = SystemStateEvent
        fields = '__all__'
