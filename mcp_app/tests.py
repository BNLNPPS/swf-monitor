import pytest
from channels.testing import WebsocketCommunicator
from swf_monitor_project.asgi import application
from monitor_app.models import SystemAgent
from django.contrib.auth.models import User
import json
import uuid
from django.utils import timezone
from datetime import timedelta, timezone as dt_timezone

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_consumer_unauthenticated_connection():
    """Test that an unauthenticated user cannot connect to the WebSocket."""
    communicator = WebsocketCommunicator(application, "/ws/mcp/")
    connected, close_code = await communicator.connect()
    assert not connected, "Unauthenticated user should not be able to connect"

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_consumer_authenticated_connection():
    """Test that an authenticated user can connect."""
    user = await User.objects.acreate(username='testuser', password='password')
    communicator = WebsocketCommunicator(application, "/ws/mcp/")
    communicator.scope['user'] = user
    connected, _ = await communicator.connect()
    assert connected, "Authenticated user should be able to connect"
    await communicator.disconnect()

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_heartbeat_notification():
    """Test that the consumer correctly processes a heartbeat notification."""
    user = await User.objects.acreate(username='testuser', password='password')
    agent = await SystemAgent.objects.acreate(
        instance_name='agent1', agent_type='type1', status='OK'
    )

    communicator = WebsocketCommunicator(application, "/ws/mcp/")
    communicator.scope['user'] = user
    await communicator.connect()

    message_id = str(uuid.uuid4())
    timestamp = timezone.now().isoformat()

    await communicator.send_to(text_data=json.dumps({
        "mcp_version": "1.0",
        "message_id": message_id,
        "command": "heartbeat",
        "payload": {
            "name": "agent1",
            "timestamp": timestamp,
            "status": "WARNING"
        }
    }))

    # Heartbeat is a notification, so we expect no direct reply.
    response = await communicator.receive_nothing(timeout=1)
    assert response is True

    await agent.arefresh_from_db()
    assert agent.status == 'WARNING'
    # The consumer converts the ISO string to a timezone-aware datetime object
    assert agent.last_heartbeat.isoformat() == timestamp

    await communicator.disconnect()

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_discover_capabilities():
    """Test the discover_capabilities command."""
    user = await User.objects.acreate(username='testuser', password='password')
    communicator = WebsocketCommunicator(application, "/ws/mcp/")
    communicator.scope['user'] = user
    await communicator.connect()

    message_id = str(uuid.uuid4())
    await communicator.send_to(text_data=json.dumps({
        "mcp_version": "1.0",
        "message_id": message_id,
        "command": "discover_capabilities",
        "payload": {}
    }))

    response = await communicator.receive_from()
    data = json.loads(response)

    assert data['status'] == 'success'
    assert data['in_reply_to'] == message_id
    assert 'discover_capabilities' in data['payload']
    assert 'get_agent_liveness' in data['payload']
    assert 'heartbeat' in data['payload']

    await communicator.disconnect()

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_get_agent_liveness():
    """Test the get_agent_liveness command returns correct agent statuses."""
    user = await User.objects.acreate(username='testuser', password='password')
    now = timezone.now()

    await SystemAgent.objects.acreate(
        instance_name='alive_agent', agent_type='type1', status='OK', 
        last_heartbeat=now - timedelta(minutes=1)
    )
    await SystemAgent.objects.acreate(
        instance_name='dead_agent', agent_type='type2', status='OK', 
        last_heartbeat=now - timedelta(minutes=10)
    )

    communicator = WebsocketCommunicator(application, "/ws/mcp/")
    communicator.scope['user'] = user
    await communicator.connect()

    message_id = str(uuid.uuid4())
    await communicator.send_to(text_data=json.dumps({
        "mcp_version": "1.0",
        "message_id": message_id,
        "command": "get_agent_liveness",
        "payload": {}
    }))

    response = await communicator.receive_from()
    data = json.loads(response)

    assert data['status'] == 'success'
    assert data['in_reply_to'] == message_id
    payload = data['payload']
    assert payload['alive_agent'] == 'alive'
    assert payload['dead_agent'] == 'dead'

    await communicator.disconnect()

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_invalid_json_message():
    """Test that the consumer handles invalid JSON messages gracefully."""
    user = await User.objects.acreate(username='testuser', password='password')
    communicator = WebsocketCommunicator(application, "/ws/mcp/")
    communicator.scope['user'] = user
    await communicator.connect()

    await communicator.send_to(text_data="not a valid json")
    
    response = await communicator.receive_from()
    data = json.loads(response)

    assert data['status'] == 'error'
    assert data['error']['code'] == 4000
    assert 'Invalid JSON' in data['error']['message']

    await communicator.disconnect()

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_missing_mcp_version():
    """Test that messages missing the mcp_version field are rejected."""
    user = await User.objects.acreate(username='testuser', password='password')
    communicator = WebsocketCommunicator(application, "/ws/mcp/")
    communicator.scope['user'] = user
    await communicator.connect()

    message_id = str(uuid.uuid4())
    await communicator.send_to(text_data=json.dumps({
        "message_id": message_id,
        "command": "discover_capabilities",
        "payload": {}
    }))

    response = await communicator.receive_from()
    data = json.loads(response)

    assert data['status'] == 'error'
    assert data['error']['code'] == 4001
    assert 'mcp_version' in data['error']['message']

    await communicator.disconnect()
