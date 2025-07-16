from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    SystemAgentViewSet, AppLogViewSet, LogSummaryView,
    STFWorkflowViewSet, AgentWorkflowStageViewSet, WorkflowMessageViewSet
)

router = DefaultRouter()
router.register(r'systemagents', SystemAgentViewSet, basename='systemagent')
router.register(r'logs', AppLogViewSet, basename='applog')
router.register(r'workflows', STFWorkflowViewSet, basename='stfworkflow')
router.register(r'workflow-stages', AgentWorkflowStageViewSet, basename='agentworkflowstage')
router.register(r'workflow-messages', WorkflowMessageViewSet, basename='workflowmessage')

urlpatterns = [
    path('logs/summary/', LogSummaryView.as_view(), name='log-summary'),
    path('', include(router.urls)),
]
