import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from monitor_app.models import SystemAgent

class MCPConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope["user"]
        if not self.user or not self.user.is_authenticated:
            await self.close()
            return

        await self.accept()
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': f'MCP WebSocket connection established for user {self.user.username}'
        }))

    async def disconnect(self, close_code):
        pass

    async def receive(self, text_data):
        user = self.scope['user']
        print(f"MCP received message from {user.username}: {text_data}")
        try:
            data = json.loads(text_data)
            command = data.get("command")

            if command == "heartbeat":
                await self.send_heartbeat(data)
            else:
                await self.send_error(f"Unknown command: {command}")
        except json.JSONDecodeError:
            await self.send_error("Invalid JSON received.")
        except Exception as e:
            await self.send_error(f"An unexpected error occurred: {str(e)}")

    @database_sync_to_async
    def update_agent_status(self, agent_id, status):
        """Updates the status of an agent."""
        try:
            agent = SystemAgent.objects.get(instance_name=agent_id)
            agent.status = status
            agent.save()
            return {
                'id': agent.id,
                'instance_name': agent.instance_name,
                'agent_type': agent.agent_type,
                'status': agent.status,
                'last_heartbeat': agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
                'agent_url': agent.agent_url
            }
        except SystemAgent.DoesNotExist:
            return None

    async def send_heartbeat(self, data):
        agent_id = data.get('agent_id')
        status = data.get('status')
        if not agent_id or not status:
            await self.send_error("Missing agent_id or status for heartbeat.")
            return

        updated_agent = await self.update_agent_status(agent_id, status)

        if updated_agent:
            await self.send(text_data=json.dumps({
                'type': 'heartbeat',
                'agent': updated_agent
            }))
        else:
            await self.send_error(f"Agent with id {agent_id} not found.")

    async def send_error(self, message):
        await self.send(text_data=json.dumps({
            'type': 'error',
            'message': message
        }))
