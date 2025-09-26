from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    SystemAgentViewSet, AppLogViewSet, LogSummaryView,
    STFWorkflowViewSet, AgentWorkflowStageViewSet, WorkflowMessageViewSet,
    RunViewSet, StfFileViewSet, SubscriberViewSet, FastMonFileViewSet,
    get_next_run_number, get_next_agent_id
)
from .sse_views import sse_message_stream, sse_status

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

urlpatterns = [
    path('logs/summary/', LogSummaryView.as_view(), name='log-summary'),
    path('state/next-run-number/', get_next_run_number, name='get-next-run-number'),
    path('state/next-agent-id/', get_next_agent_id, name='get-next-agent-id'),
    path('messages/stream/', sse_message_stream, name='sse-message-stream'),
    path('messages/stream/status/', sse_status, name='sse-stream-status'),
    path('', include(router.urls)),
]
