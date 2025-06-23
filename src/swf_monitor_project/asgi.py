"""
ASGI config for swf_monitor_project project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/asgi/
"""

import os

# Ensure DJANGO_SETTINGS_MODULE is set before any Django imports
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django
from django.core.asgi import get_asgi_application

# Initialize Django settings
django.setup()

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
import mcp_app.routing # This import might trigger settings access if consumers.py imports models

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": AuthMiddlewareStack(
        URLRouter(
            mcp_app.routing.websocket_urlpatterns
        )
    ),
})
