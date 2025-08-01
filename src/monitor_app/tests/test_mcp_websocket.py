#!/usr/bin/env python3
"""
Test script for MCP WebSocket endpoint
Tests the WebSocket connection at ws://localhost:8002/ws/mcp/
"""

import asyncio
import json
import websockets
import logging
import requests
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# MCP WebSocket endpoint
MCP_WS_URL = "ws://localhost:8002/ws/mcp/"

# Test messages based on actual MCP implementation
TEST_MESSAGES = [
    # 1. Discover capabilities
    {
        "mcp_version": "1.0",
        "message_id": "discover-1",
        "command": "discover_capabilities",
        "payload": {}
    },
    # 2. Get agent liveness status
    {
        "mcp_version": "1.0",
        "message_id": "liveness-1", 
        "command": "get_agent_liveness",
        "payload": {}
    },
    # 3. Send a heartbeat notification
    {
        "mcp_version": "1.0",
        "message_id": "heartbeat-1",
        "command": "heartbeat",
        "payload": {
            "name": "test-agent",
            "timestamp": datetime.now().isoformat(),
            "status": "OK"
        }
    },
    # 4. Invalid command to test error handling
    {
        "mcp_version": "1.0",
        "message_id": "error-1",
        "command": "invalid_command",
        "payload": {}
    }
]

def get_django_session_cookies():
    """Login to Django and get session cookies for WebSocket authentication"""
    login_url = 'http://localhost:8002/admin/login/'
    session = requests.Session()
    session.proxies = {'http': None, 'https': None}  # Bypass proxy for localhost
    
    # Get CSRF token
    response = session.get(login_url)
    if response.status_code != 200:
        raise Exception(f"Failed to get login page: {response.status_code}")
    
    # Extract CSRF token from response
    csrf_token = None
    for line in response.text.split('\n'):
        if 'csrfmiddlewaretoken' in line and 'value=' in line:
            csrf_token = line.split('value="')[1].split('"')[0]
            break
    
    if not csrf_token:
        raise Exception("Could not find CSRF token")
    
    # Login with testuser credentials
    login_data = {
        'username': 'testuser',
        'password': 'test.user?724',
        'csrfmiddlewaretoken': csrf_token,
        'next': '/admin/'
    }
    
    response = session.post(login_url, data=login_data)
    if 'admin' not in response.url:
        raise Exception(f"Login failed: {response.status_code}, URL: {response.url}")
    
    # Return cookies for WebSocket connection
    cookies = '; '.join([f"{cookie.name}={cookie.value}" for cookie in session.cookies])
    return cookies

async def test_mcp_websocket():
    """Test the MCP WebSocket endpoint with various messages"""
    
    logger.info("Getting Django session cookies...")
    try:
        cookies = get_django_session_cookies()
        logger.info("✅ Successfully authenticated with Django")
    except Exception as e:
        logger.error(f"❌ Authentication failed: {e}")
        return
    
    logger.info(f"Connecting to MCP WebSocket at {MCP_WS_URL}")
    
    try:
        # Connect with session cookies for authentication
        async with websockets.connect(MCP_WS_URL, additional_headers={'Cookie': cookies}) as websocket:
            logger.info("✅ Successfully connected to MCP WebSocket")
            
            for i, message in enumerate(TEST_MESSAGES, 1):
                logger.info(f"\n--- Test {i}: {message.get('command', 'unknown')} ---")
                logger.info(f"Sending: {json.dumps(message, indent=2)}")
                
                # Send the message
                await websocket.send(json.dumps(message))
                
                # For heartbeat, we don't expect a response
                if message.get('command') == 'heartbeat':
                    logger.info("📤 Heartbeat sent (no response expected)")
                    await asyncio.sleep(0.5)
                else:
                    # Wait for response
                    try:
                        response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                        response_data = json.loads(response)
                        logger.info(f"Response: {json.dumps(response_data, indent=2)}")
                        
                        # Validate response
                        if response_data.get("status") == "error":
                            logger.warning(f"Error response: {response_data.get('error')}")
                        elif response_data.get("status") == "success":
                            logger.info("✅ Successful response received")
                    except asyncio.TimeoutError:
                        logger.error("❌ Timeout waiting for response")
            
            logger.info("\n✅ All tests completed")
            
    except websockets.exceptions.WebSocketException as e:
        logger.error(f"❌ WebSocket error: {type(e).__name__}: {e}")
    except Exception as e:
        logger.error(f"❌ Unexpected error: {type(e).__name__}: {e}")

async def test_authentication():
    """Test WebSocket connection with authentication"""
    logger.info("\n--- Testing authenticated connection ---")
    
    # In production, you'd get this from environment or config
    auth_headers = {
        "Authorization": "Bearer test-token"  # Replace with actual auth token
    }
    
    try:
        async with websockets.connect(MCP_WS_URL, extra_headers=auth_headers) as websocket:
            logger.info("✅ Connected with authentication")
            
            # Send a simple test message
            test_msg = {
                "jsonrpc": "2.0",
                "method": "tools/list",
                "params": {},
                "id": "auth-test-1"
            }
            
            await websocket.send(json.dumps(test_msg))
            response = await websocket.recv()
            logger.info(f"Authenticated response: {response}")
            
    except websockets.exceptions.WebSocketException as e:
        logger.error(f"❌ Authentication failed: {e}")

if __name__ == "__main__":
    logger.info("Starting MCP WebSocket tests...")
    
    # Run the main test
    asyncio.run(test_mcp_websocket())
    
    # Optionally test authentication
    # asyncio.run(test_authentication())
    
    logger.info("\nTests complete!")