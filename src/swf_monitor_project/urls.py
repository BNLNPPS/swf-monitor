"""
URL configuration for swf_monitor_project project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from monitor_app.views import oauth_protected_resource

urlpatterns = [
    path(".well-known/oauth-protected-resource", oauth_protected_resource, name="oauth_protected_resource"),
    path("admin/", admin.site.urls),
    # MCP is no longer served by Django. The /swf-monitor/mcp/ path is
    # owned by the standalone FastMCP ASGI service (see
    # swf_monitor_project/mcp_asgi.py and swf-monitor-mcp-asgi.service).
    # Apache ProxyPass routes it directly to 127.0.0.1:8001 ahead of the
    # WSGIScriptAlias, so Django never sees /mcp/ traffic.
    path("o/", include("oauth2_provider.urls", namespace="oauth2_provider")),  # OAuth2 for MCP
    path("api-auth/", include("rest_framework.urls")),
    path("accounts/", include("django.contrib.auth.urls")),  # Add this line
    path("pcs/", include("pcs.urls")),  # Physics Configuration System
    path("", include("monitor_app.urls")),  # Include monitor_app URLs for the root path
    # API Schema and Documentation
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    # Optional UI:
    path('api/schema/swagger-ui/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/schema/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]
