from django.contrib import admin
from .models import SystemAgent
from .workflow_models import WorkflowDefinition, WorkflowExecution

@admin.register(SystemAgent)
class SystemAgentAdmin(admin.ModelAdmin):
    list_display = ('instance_name', 'agent_type', 'status', 'last_heartbeat', 'agent_url')
    list_filter = ('status', 'agent_type')
    search_fields = ('instance_name', 'agent_type', 'agent_url')


@admin.register(WorkflowDefinition)
class WorkflowDefinitionAdmin(admin.ModelAdmin):
    list_display = ('workflow_name', 'version', 'workflow_type', 'created_by', 'created_at')
    list_filter = ('workflow_type', 'created_by')
    search_fields = ('workflow_name', 'workflow_type', 'created_by')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(WorkflowExecution)
class WorkflowExecutionAdmin(admin.ModelAdmin):
    list_display = ('execution_id', 'workflow_definition', 'status', 'executed_by', 'start_time')
    list_filter = ('status', 'executed_by')
    search_fields = ('execution_id', 'executed_by')
    readonly_fields = ('start_time', 'end_time')
