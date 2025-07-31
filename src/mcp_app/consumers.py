import json
import uuid
from datetime import datetime
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from datetime import timedelta
from monitor_app.models import SystemAgent
from .views import MCPService

class MCPConsumer(AsyncWebsocketConsumer):
    MCP_VERSION = "1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.capabilities = MCPService.get_capabilities()

    async def connect(self):
        # Accept all connections (no authentication required for R&D/demo)
        print(f"DEBUG: WebSocket connection accepted (no auth required)")
        await self.accept()

    async def disconnect(self, close_code):
        pass

    async def receive(self, text_data):
        message_id = None
        try:
            data = json.loads(text_data)
            message_id = data.get("message_id")

            if 'mcp_version' not in data:
                await self.send_error_response(4001, "'mcp_version' is a required field.", message_id)
                return
            
            if data['mcp_version'] != self.MCP_VERSION:
                await self.send_error_response(4002, f"Unsupported MCP version. Server supports {self.MCP_VERSION}.", message_id)
                return

            if not all(k in data for k in ['message_id', 'command', 'payload']):
                await self.send_error_response(4000, "Invalid MCP message format. 'message_id', 'command', and 'payload' are required.", message_id)
                return

            command = data["command"]
            payload = data["payload"]

            if command == "heartbeat":
                await self.handle_heartbeat(payload)
            elif command == "discover_capabilities":
                await self.send_response('success', self.capabilities, message_id)
            elif command == "get_agent_liveness":
                liveness_payload = await self.get_agent_liveness()
                await self.send_response('success', liveness_payload, message_id)
            else:
                await self.send_error_response(4004, f"Unknown command: {command}", message_id)

        except json.JSONDecodeError:
            await self.send_error_response(4000, "Invalid JSON format.")
        except Exception as e:
            # Generic error for unexpected issues
            await self.send_error_response(5000, f"An unexpected server error occurred: {str(e)}", message_id)

    @database_sync_to_async
    def handle_heartbeat(self, payload):
        try:
            MCPService.handle_heartbeat(payload)
        except ValueError as e:
            print(f"MCP: Heartbeat error: {e}")

    @database_sync_to_async
    def get_agent_liveness(self):
        return MCPService.get_agent_liveness()

    async def send_response(self, status, payload, in_reply_to):
        response = MCPService.create_response(status, payload, in_reply_to)
        await self.send(text_data=json.dumps(response))

    async def send_error_response(self, code, message, in_reply_to=None):
        response = MCPService.create_error_response(code, message, in_reply_to)
        await self.send(text_data=json.dumps(response))
