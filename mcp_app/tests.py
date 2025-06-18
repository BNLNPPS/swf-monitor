import pytest
from channels.testing import WebsocketCommunicator
from swf_monitor_project.asgi import application
from monitor_app.models import MonitoredItem
from django.contrib.auth.models import User
import json

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_consumer_unauthenticated_connection():
    """Test that an unauthenticated user cannot connect to the WebSocket."""
    communicator = WebsocketCommunicator(application, "/ws/mcp/")
    connected, close_code = await communicator.connect()
    assert not connected
    # Optionally, check the close code if your consumer sets a specific one

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_consumer_authenticated_flow():
    """Test the full WebSocket flow for an authenticated user."""
    # Create a test user
    user = await User.objects.acreate(username='testuser')

    # Create test data
    await MonitoredItem.objects.acreate(name="agent1", status="OK", agent_url="http://agent1.com")
    await MonitoredItem.objects.acreate(name="agent2", status="WARNING", agent_url="http://agent2.com")

    communicator = WebsocketCommunicator(application, "/ws/mcp/")
    communicator.scope['user'] = user  # Simulate an authenticated user
    connected, _ = await communicator.connect()
    assert connected, "Authenticated user should be able to connect"

    # Test connection established message
    response = await communicator.receive_from()
    data = json.loads(response)
    assert data['type'] == 'connection_established'
    assert user.username in data['message']

    # Test get_all_statuses
    await communicator.send_to(text_data=json.dumps({"command": "get_all_statuses"}))
    response = await communicator.receive_from()
    data = json.loads(response)
    assert data['command'] == 'all_statuses'
    assert len(data['data']) == 2
    assert data['data'][0]['name'] in ["agent1", "agent2"]

    # Test get_agent_status for existing agent
    await communicator.send_to(text_data=json.dumps({"command": "get_agent_status", "agent_id": "agent1"}))
    response = await communicator.receive_from()
    data = json.loads(response)
    assert data['command'] == 'agent_status'
    assert data['agent_id'] == "agent1"
    assert data['data']['name'] == "agent1"
    assert data['data']['status'] == "OK"

    # Test get_agent_status for non-existent agent
    await communicator.send_to(text_data=json.dumps({"command": "get_agent_status", "agent_id": "nonexistent"}))
    response = await communicator.receive_from()
    data = json.loads(response)
    assert data['command'] == 'agent_status'
    assert data['agent_id'] == "nonexistent"
    assert data['error'] == 'Agent not found'

    # Test unknown command
    await communicator.send_to(text_data=json.dumps({"command": "unknown_command"}))
    response = await communicator.receive_from()
    data = json.loads(response)
    assert data['type'] == 'error'
    assert data['message'] == 'Unknown command: unknown_command'

    # Test invalid JSON
    await communicator.send_to(text_data="not a valid json")
    response = await communicator.receive_from()
    data = json.loads(response)
    assert data['type'] == 'error'
    assert data['message'] == 'Invalid JSON received.'

    # Close the connection
    await communicator.disconnect()
