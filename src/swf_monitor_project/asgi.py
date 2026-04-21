"""
ASGI config for swf_monitor_project project.

Exposes the ASGI callable as a module-level variable named ``application``.

Used by the uvicorn worker (see swf-monitor-mcp-asgi.service) that serves
/swf-monitor/mcp/ behind Apache ProxyPass. Pure HTTP — no WebSocket consumers
exist in this codebase; the Channels layer is used only for inter-process SSE
fan-out inside the WSGI app (see docs/SSE_RELAY.md).
"""

import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django
from django.core.asgi import get_asgi_application

django.setup()

application = get_asgi_application()
