import json
import uuid
from datetime import datetime
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from datetime import timedelta
from monitor_app.models import SystemAgent

class MCPConsumer(AsyncWebsocketConsumer):
    MCP_VERSION = "1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.capabilities = {
            'discover_capabilities': 'Lists all available commands and their descriptions.',
            'get_agent_liveness': 'Returns a report of all agents, categorized as alive or dead based on recent heartbeats.',
            'heartbeat': 'A notification sent by an agent to signal it is still active. Does not receive a direct response.'
        }

    async def connect(self):
        self.user = self.scope.get("user")
        if not self.user or not self.user.is_authenticated:
            await self.close(code=4003)
            return
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
        agent_name = payload.get('name')
        timestamp_str = payload.get('timestamp')
        status = payload.get('status')

        if not all([agent_name, timestamp_str, status]):
            # This is a notification, so we don't send an error response, just log it.
            print(f"MCP: Received incomplete heartbeat payload: {payload}")
            return

        try:
            agent = SystemAgent.objects.get(instance_name=agent_name)
            agent.status = status
            agent.last_heartbeat = datetime.fromisoformat(timestamp_str)
            agent.save()
        except SystemAgent.DoesNotExist:
            print(f"MCP: Received heartbeat for unknown agent: {agent_name}")
        except ValueError:
            print(f"MCP: Invalid timestamp format in heartbeat: {timestamp_str}")

    @database_sync_to_async
    def get_agent_liveness(self):
        alive_threshold = timezone.now() - timedelta(minutes=5)
        agents = SystemAgent.objects.all()
        liveness_status = {}
        for agent in agents:
            if agent.last_heartbeat and agent.last_heartbeat >= alive_threshold:
                liveness_status[agent.instance_name] = 'alive'
            else:
                liveness_status[agent.instance_name] = 'dead'
        return liveness_status

    async def send_response(self, status, payload, in_reply_to):
        response = {
            "mcp_version": self.MCP_VERSION,
            "message_id": str(uuid.uuid4()),
            "in_reply_to": in_reply_to,
            "status": status,
            "payload": payload,
        }
        await self.send(text_data=json.dumps(response))

    async def send_error_response(self, code, message, in_reply_to=None):
        response = {
            "mcp_version": self.MCP_VERSION,
            "message_id": str(uuid.uuid4()),
            "status": "error",
            "error": {
                "code": code,
                "message": message,
            },
        }
        if in_reply_to:
            response["in_reply_to"] = in_reply_to
        await self.send(text_data=json.dumps(response))
