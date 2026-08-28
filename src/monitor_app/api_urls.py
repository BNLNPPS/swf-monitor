from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    SystemAgentViewSet, AppLogViewSet, LogSummaryView,
    STFWorkflowViewSet, AgentWorkflowStageViewSet, WorkflowMessageViewSet,
    RunViewSet, StfFileViewSet, SubscriberViewSet, FastMonFileViewSet,
    WorkflowDefinitionViewSet, WorkflowExecutionViewSet,
    TFSliceViewSet, WorkerViewSet, RunStateViewSet, SystemStateEventViewSet,
    get_next_run_number, get_next_agent_id, get_next_workflow_execution_id,
    ensure_namespace,
    ai_memory_record, ai_memory_load, dpid_verify, panda_slash_command,
    users_list,
)
from .sse_views import sse_message_stream, sse_status
from .panda import api as panda_api
from .panda.corun_callback import corun_callback
from .viewdir.capcom import (capcom_notice_ingest, capcom_notices,
                             capcom_state, capcom_user_state,
                             NoticeSubscriptionViewSet)
from .viewdir.snapper_api import (snapper_changes_between,
                                  snapper_component_history, snapper_context,
                                  snapper_cut_summary, snapper_latest,
                                  snapper_series, snapper_state_at,
                                  system_status_history)
from .viewdir.snapper_episodes_api import (episode_detail_view,
                                           episodes_append, episodes_close,
                                           episodes_list_view, episodes_open)

router = DefaultRouter()
router.register(r'systemagents', SystemAgentViewSet, basename='systemagent')
router.register(r'logs', AppLogViewSet, basename='applog')
router.register(r'workflows', STFWorkflowViewSet, basename='stfworkflow')
router.register(r'workflow-stages', AgentWorkflowStageViewSet, basename='agentworkflowstage')
router.register(r'workflow-messages', WorkflowMessageViewSet, basename='workflowmessage')
router.register(r'runs', RunViewSet, basename='run')
router.register(r'stf-files', StfFileViewSet, basename='stffile')
router.register(r'subscribers', SubscriberViewSet, basename='subscriber')
router.register(r'fastmon-files', FastMonFileViewSet, basename='fastmonfile')
router.register(r'workflow-definitions', WorkflowDefinitionViewSet, basename='workflowdefinition')
router.register(r'workflow-executions', WorkflowExecutionViewSet, basename='workflowexecution')

# Fast Processing API endpoints
router.register(r'tf-slices', TFSliceViewSet, basename='tfslice')
router.register(r'workers', WorkerViewSet, basename='worker')
router.register(r'run-states', RunStateViewSet, basename='runstate')
router.register(r'system-state-events', SystemStateEventViewSet, basename='systemstateevent')
router.register(r'notices/subscriptions', NoticeSubscriptionViewSet, basename='noticesubscription')

urlpatterns = [
    path('logs/summary/', LogSummaryView.as_view(), name='log-summary'),
    path('state/next-run-number/', get_next_run_number, name='get-next-run-number'),
    path('state/next-agent-id/', get_next_agent_id, name='get-next-agent-id'),
    path('state/next-workflow-execution-id/', get_next_workflow_execution_id, name='get-next-workflow-execution-id'),
    path('namespaces/ensure/', ensure_namespace, name='ensure-namespace'),
    path('ai-memory/record/', ai_memory_record, name='ai-memory-record'),
    path('ai-memory/', ai_memory_load, name='ai-memory-load'),
    path('dpid/verify/', dpid_verify, name='dpid-verify'),
    path('slash/panda/', panda_slash_command, name='panda-slash-command'),
    path('corun-callback/', corun_callback, name='corun-callback'),
    # PanDA REST API — read-only JSON for external consumers.
    # See monitor_app/panda/api.py.
    path('panda/tasks/', panda_api.tasks_list, name='panda-api-tasks-list'),
    path('panda/tasks/<int:jeditaskid>/', panda_api.task_detail, name='panda-api-task-detail'),
    path('panda/tasks/<int:jeditaskid>/operations/',
         panda_api.task_operation_request,
         name='panda-task-operation-request'),
    path('panda/task-operations/',
         panda_api.task_operations_request,
         name='panda-task-operations-request'),
    path('panda/task-operations/<uuid:operation_id>/',
         panda_api.task_operation_detail,
         name='panda-task-operation-detail'),
    path('panda/task-operations/<uuid:operation_id>/state/',
         panda_api.task_operation_update,
         name='panda-task-operation-update'),
    path('panda/jobs/', panda_api.jobs_list, name='panda-api-jobs-list'),
    path('panda/activity/', panda_api.activity, name='panda-api-activity'),
    path('users/', users_list, name='users-list'),
    # Episode ingest (token-authenticated writes from the episode
    # builder agent) and read surfaces; snapper_episodes_api.py.
    path('snapper/episodes/open/', episodes_open, name='snapper-episodes-open'),
    path('snapper/episodes/append/', episodes_append,
         name='snapper-episodes-append'),
    path('snapper/episodes/close/', episodes_close,
         name='snapper-episodes-close'),
    path('snapper/<str:scope>/episodes/', episodes_list_view,
         name='snapper-episodes-list'),
    path('snapper/<str:scope>/episodes/<str:episode_id>/',
         episode_detail_view, name='snapper-episode-detail'),
    path('snapper/<str:scope>/latest/', snapper_latest,
         name='snapper-latest'),
    path('snapper/<str:scope>/state-at/', snapper_state_at,
         name='snapper-state-at'),
    path('snapper/<str:scope>/history/', snapper_component_history,
         name='snapper-component-history'),
    path('snapper/<str:scope>/changes/', snapper_changes_between,
         name='snapper-changes-between'),
    path('snapper/<str:scope>/context/', snapper_context,
         name='snapper-context'),
    path('snapper/<str:scope>/series/', snapper_series,
         name='snapper-series'),
    path('snapper/<str:scope>/cut-summary/', snapper_cut_summary,
         name='snapper-cut-summary'),
    path('system-status/history/', system_status_history,
         name='system-status-history'),
    path('capcom/state/', capcom_state, name='capcom-state'),
    path('capcom/user-state/', capcom_user_state, name='capcom-user-state'),
    path('capcom/notices/', capcom_notices, name='capcom-notices'),
    path('capcom/notices/ingest/', capcom_notice_ingest,
         name='capcom-notice-ingest'),
    path('messages/stream/', sse_message_stream, name='sse-message-stream'),
    path('messages/stream/status/', sse_status, name='sse-stream-status'),
    path('', include(router.urls)),
]
