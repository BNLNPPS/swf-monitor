from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import SystemAgentViewSet

router = DefaultRouter()
router.register(r'systemagents', SystemAgentViewSet, basename='systemagent')

urlpatterns = [
    path('', include(router.urls)),
]
