import pytest
from channels.testing import WebsocketCommunicator
from swf_monitor_project.asgi import application
from monitor_app.models import SystemAgent
from django.contrib.auth.models import User
import json
from unittest.mock import patch, MagicMock

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
    user = await User.objects.acreate(username='testuser', password='password')

    # Create test data
    agent = await SystemAgent.objects.acreate(instance_name='agent1', agent_type='type1', status='OK')

    communicator = WebsocketCommunicator(application, "/ws/mcp/")
    communicator.scope['user'] = user  # Simulate an authenticated user
    connected, _ = await communicator.connect()
    assert connected, "Authenticated user should be able to connect"

    # Test connection established message
    response = await communicator.receive_from()
    data = json.loads(response)
    assert data['type'] == 'connection_established'
    assert user.username in data['message']

    # Test heartbeat message
    await communicator.send_to(text_data=json.dumps({
        "command": "heartbeat",
        "agent_id": "agent1",
        "status": "WARNING"
    }))
    response = await communicator.receive_from()
    data = json.loads(response)
    assert data['type'] == 'heartbeat'
    assert data['agent']['instance_name'] == 'agent1'
    assert data['agent']['status'] == 'WARNING'

    # Verify the database was updated
    await agent.arefresh_from_db()
    assert agent.status == 'WARNING'

    # Close the connection
    await communicator.disconnect()

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_consumer_invalid_message():
    """Test that the consumer handles invalid JSON messages gracefully."""
    user = await User.objects.acreate(username='testuser', password='password')
    communicator = WebsocketCommunicator(application, "/ws/mcp/")
    communicator.scope['user'] = user
    connected, _ = await communicator.connect()
    assert connected

    # Send an invalid JSON message
    await communicator.send_to(text_data="not a valid json")
    # The consumer should not disconnect and we should not receive a message
    # It will simply log an error and continue
    # We can check that the connection is still open
    response = await communicator.receive_nothing(timeout=1)
    assert response is True

    await communicator.disconnect()

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_consumer_database_error():
    """Test that the consumer handles database errors gracefully."""
    user = await User.objects.acreate(username='testuser', password='password')
    agent = await SystemAgent.objects.acreate(instance_name='agent1', agent_type='type1', status='OK')

    communicator = WebsocketCommunicator(application, "/ws/mcp/")
    communicator.scope['user'] = user
    connected, _ = await communicator.connect()
    assert connected

    # Mock the database update to raise an exception
    with patch('monitor_app.models.SystemAgent.asave', new_callable=MagicMock) as mock_asave:
        mock_asave.side_effect = Exception("Database error")

        await communicator.send_to(text_data=json.dumps({
            "command": "heartbeat",
            "agent_id": "agent1",
            "status": "WARNING"
        }))

        # The consumer should not disconnect and we should not receive a message
        response = await communicator.receive_nothing(timeout=1)
        assert response is True

    await communicator.disconnect()
