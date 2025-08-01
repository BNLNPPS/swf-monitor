from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    SystemAgentViewSet, AppLogViewSet, LogSummaryView,
    STFWorkflowViewSet, AgentWorkflowStageViewSet, WorkflowMessageViewSet,
    RunViewSet, StfFileViewSet, SubscriberViewSet, MessageQueueDispatchViewSet
)

router = DefaultRouter()
router.register(r'systemagents', SystemAgentViewSet, basename='systemagent')
router.register(r'logs', AppLogViewSet, basename='applog')
router.register(r'workflows', STFWorkflowViewSet, basename='stfworkflow')
router.register(r'workflow-stages', AgentWorkflowStageViewSet, basename='agentworkflowstage')
router.register(r'workflow-messages', WorkflowMessageViewSet, basename='workflowmessage')
router.register(r'runs', RunViewSet, basename='run')
router.register(r'stf-files', StfFileViewSet, basename='stffile')
router.register(r'subscribers', SubscriberViewSet, basename='subscriber')
router.register(r'message-dispatches', MessageQueueDispatchViewSet, basename='messagedispatch')

urlpatterns = [
    path('logs/summary/', LogSummaryView.as_view(), name='log-summary'),
    path('', include(router.urls)),
]
