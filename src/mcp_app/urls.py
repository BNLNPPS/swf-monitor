from django.urls import path
from . import views

app_name = 'mcp'

urlpatterns = [
    path('heartbeat/', views.heartbeat, name='heartbeat'),
    path('discover-capabilities/', views.discover_capabilities, name='discover_capabilities'),
    path('agent-liveness/', views.get_agent_liveness, name='get_agent_liveness'),
]