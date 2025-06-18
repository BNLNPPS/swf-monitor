import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from monitor_app.models import MonitoredItem # Adjusted import path

class MCPConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # Perform connection setup, e.g., authentication, logging
        # For now, just accept all connections
        await self.accept()
        print(f"MCP WebSocket connection established: {self.channel_name}")
        # Optionally, send an initial message or available commands
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': 'Welcome to the SWF Monitor MCP Service!',
            # Corrected to show agent_id
            'available_commands': ['get_all_statuses', 'get_agent_status (expects {"command": "get_agent_status", "agent_id": "your_agent_id"})']
        }))

    async def disconnect(self, close_code):
        print(f"MCP WebSocket connection closed: {self.channel_name}")
        # Perform cleanup if needed
        pass

    async def receive(self, text_data):
        print(f"MCP received message: {text_data}")
        try:
            data = json.loads(text_data)
            command = data.get("command")
            # Removed params nesting, directly get agent_id from top level
            agent_id = data.get("agent_id")

            if command == "get_all_statuses":
                await self.get_all_statuses()
            elif command == "get_agent_status":
                if agent_id: # Check for agent_id directly
                    await self.get_agent_status(agent_id)
                else:
                    # Corrected error message to reflect agent_id
                    await self.send_error("Missing agent_id parameter for get_agent_status")
            else:
                await self.send_error(f"Unknown command: {command}")
        except json.JSONDecodeError:
            await self.send_error("Invalid JSON received.")
        except Exception as e:
            await self.send_error(f"An error occurred: {str(e)}")

    @database_sync_to_async
    def _get_all_monitored_items(self):
        items = MonitoredItem.objects.all()
        # Serialize queryset to a list of dicts
        return list(items.values('name', 'status', 'last_heartbeat', 'agent_url', 'updated_at'))

    @database_sync_to_async
    def _get_monitored_item_by_name(self, name): # Assuming agent_id corresponds to the 'name' field
        try:
            item = MonitoredItem.objects.get(name=name)
            # Serialize single object to a dict
            return {
                'name': item.name,
                'status': item.status,
                'last_heartbeat': item.last_heartbeat,
                'agent_url': item.agent_url,
                'updated_at': item.updated_at,
            }
        except MonitoredItem.DoesNotExist:
            return None

    async def get_all_statuses(self):
        items_data = await self._get_all_monitored_items()
        # Convert datetime objects to string if they are not JSON serializable by default
        for item in items_data:
            if item.get('last_heartbeat'):
                item['last_heartbeat'] = item['last_heartbeat'].isoformat()
            if item.get('updated_at'):
                item['updated_at'] = item['updated_at'].isoformat()

        await self.send(text_data=json.dumps({
            'command': 'all_statuses',
            'data': items_data
        }))

    async def get_agent_status(self, agent_id):
        item_data = await self._get_monitored_item_by_name(agent_id)
        if item_data:
            # Convert datetime objects to string
            if item_data.get('last_heartbeat'):
                item_data['last_heartbeat'] = item_data['last_heartbeat'].isoformat()
            if item_data.get('updated_at'):
                item_data['updated_at'] = item_data['updated_at'].isoformat()
            await self.send(text_data=json.dumps({
                'command': 'agent_status',
                'agent_id': agent_id,
                'data': item_data
            }))
        else:
            await self.send(text_data=json.dumps({
                'command': 'agent_status',
                'agent_id': agent_id,
                'error': 'Agent not found'
            }))

    async def send_error(self, message):
        await self.send(text_data=json.dumps({
            'type': 'error',
            'message': message
        }))

    # ... any other MCP commands can be added here ...
