#!/usr/bin/env python3
"""
Test script for MCP REST API endpoints.
Verifies that all MCP REST endpoints respond correctly.
"""

import requests
import json
import uuid
from datetime import datetime


def test_mcp_rest_endpoints():
    """Test all MCP REST API endpoints."""
    base_url = "http://localhost:8002/api/mcp"
    
    print("🧪 Testing MCP REST API endpoints...")
    print(f"Base URL: {base_url}")
    
    # Test data
    message_id = str(uuid.uuid4())
    
    # Test 1: Discover Capabilities
    print("\n1️⃣ Testing discover-capabilities endpoint...")
    capabilities_payload = {
        "mcp_version": "1.0",
        "message_id": message_id,
        "command": "discover_capabilities",
        "payload": {}
    }
    
    try:
        response = requests.post(
            f"{base_url}/discover-capabilities/",
            json=capabilities_payload,
            headers={"Content-Type": "application/json"}
        )
        print(f"   Status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print(f"   ✅ Capabilities: {list(data.get('payload', {}).keys())}")
        else:
            print(f"   ❌ Error: {response.text}")
    except requests.exceptions.ConnectionError:
        print("   ⚠️  Server not running - start with: python manage.py runserver 8002")
        return False
    
    # Test 2: Get Agent Liveness
    print("\n2️⃣ Testing agent-liveness endpoint...")
    liveness_payload = {
        "mcp_version": "1.0", 
        "message_id": str(uuid.uuid4()),
        "command": "get_agent_liveness",
        "payload": {}
    }
    
    try:
        response = requests.post(
            f"{base_url}/agent-liveness/",
            json=liveness_payload,
            headers={"Content-Type": "application/json"}
        )
        print(f"   Status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            agent_count = len(data.get('payload', {}))
            print(f"   ✅ Found {agent_count} agents in system")
        else:
            print(f"   ❌ Error: {response.text}")
    except requests.exceptions.ConnectionError:
        print("   ⚠️  Server not running")
        return False
    
    # Test 3: Heartbeat (notification)
    print("\n3️⃣ Testing heartbeat endpoint...")
    heartbeat_payload = {
        "mcp_version": "1.0",
        "message_id": str(uuid.uuid4()),
        "command": "heartbeat", 
        "payload": {
            "name": "test-agent-rest",
            "timestamp": datetime.now().isoformat(),
            "status": "OK"
        }
    }
    
    try:
        response = requests.post(
            f"{base_url}/heartbeat/",
            json=heartbeat_payload,
            headers={"Content-Type": "application/json"}
        )
        print(f"   Status: {response.status_code}")
        if response.status_code == 200:
            print("   ✅ Heartbeat processed successfully")
        else:
            print(f"   ❌ Error: {response.text}")
    except requests.exceptions.ConnectionError:
        print("   ⚠️  Server not running")
        return False
    
    # Test 4: Invalid request format
    print("\n4️⃣ Testing error handling...")
    invalid_payload = {
        "invalid_field": "test"
    }
    
    try:
        response = requests.post(
            f"{base_url}/discover-capabilities/",
            json=invalid_payload,
            headers={"Content-Type": "application/json"}
        )
        print(f"   Status: {response.status_code}")
        if response.status_code == 400:
            print("   ✅ Error handling works correctly")
        else:
            print(f"   ⚠️  Unexpected response: {response.text}")
    except requests.exceptions.ConnectionError:
        print("   ⚠️  Server not running")
        return False
    
    print("\n🎉 MCP REST API test completed!")
    return True


if __name__ == "__main__":
    print("MCP REST API Test Script")
    print("=" * 40)
    success = test_mcp_rest_endpoints()
    
    if success:
        print("\n✅ All tests completed. Check the output above for results.")
    else:
        print("\n❌ Tests failed. Make sure the Django server is running:")
        print("   cd /path/to/swf-monitor/src")
        print("   python manage.py runserver 8002")