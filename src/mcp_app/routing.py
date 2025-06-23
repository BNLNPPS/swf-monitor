from django.urls import path
from . import consumers

websocket_urlpatterns = [
    path("ws/mcp/", consumers.MCPConsumer.as_asgi()), # Example path, adjust as needed
]
