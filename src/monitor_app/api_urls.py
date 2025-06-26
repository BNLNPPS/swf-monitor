from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import SystemAgentViewSet, AppLogViewSet, LogSummaryView

router = DefaultRouter()
router.register(r'systemagents', SystemAgentViewSet, basename='systemagent')
router.register(r'logs', AppLogViewSet, basename='applog')

urlpatterns = [
    path('logs/summary/', LogSummaryView.as_view(), name='log-summary'),
    path('', include(router.urls)),
]
