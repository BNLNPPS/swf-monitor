"""
Test MCP REST API endpoints.
Tests the Model Control Protocol REST API implementation.
"""

import uuid
from datetime import datetime
from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework.authtoken.models import Token
from monitor_app.models import SystemAgent


class MCPRestAPITests(TestCase):
    """Test MCP REST API endpoints with proper authentication."""
    
    def setUp(self):
        """Set up test environment."""
        # Create test user and token for authenticated requests
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.token = Token.objects.create(user=self.user)
        
        # Create test client
        self.client = APIClient()
        
        # Create test system agents
        self.agent1 = SystemAgent.objects.create(
            instance_name='test-agent-1',
            agent_type='test',
            description='Test agent 1 for MCP REST',
            status='OK'
        )
        self.agent2 = SystemAgent.objects.create(
            instance_name='test-agent-2', 
            agent_type='daqsim',
            description='Test agent 2 for MCP REST',
            status='WARNING'
        )
    
    def test_mcp_discover_capabilities_endpoint(self):
        """Test MCP discover capabilities endpoint."""
        capabilities_payload = {
            "mcp_version": "1.0",
            "message_id": str(uuid.uuid4()),
            "command": "discover_capabilities",
            "payload": {}
        }
        
        response = self.client.post(
            '/api/mcp/discover-capabilities/',
            capabilities_payload,
            format='json'
        )
        
        # Should return 200 OK
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Should return proper MCP response structure
        data = response.json()
        self.assertIn('mcp_version', data)
        self.assertIn('message_id', data) 
        self.assertIn('payload', data)
        
        # Should include capabilities in payload
        capabilities = data.get('payload', {})
        self.assertIsInstance(capabilities, dict)
    
    def test_mcp_agent_liveness_endpoint(self):
        """Test MCP agent liveness endpoint."""
        liveness_payload = {
            "mcp_version": "1.0",
            "message_id": str(uuid.uuid4()),
            "command": "get_agent_liveness",
            "payload": {}
        }
        
        response = self.client.post(
            '/api/mcp/agent-liveness/',
            liveness_payload,
            format='json'
        )
        
        # Should return 200 OK
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Should return proper MCP response structure
        data = response.json()
        self.assertIn('mcp_version', data)
        self.assertIn('message_id', data)
        self.assertIn('payload', data)
        
        # Should include our test agents
        agents = data.get('payload', {})
        self.assertIsInstance(agents, dict)
        # Should have at least our 2 test agents
        self.assertGreaterEqual(len(agents), 2)
    
    def test_mcp_heartbeat_endpoint(self):
        """Test MCP heartbeat endpoint."""
        # Create agent first (MCP heartbeat updates existing agents)
        test_agent = SystemAgent.objects.create(
            instance_name='test-agent-mcp-rest',
            agent_type='test',
            description='Test agent for MCP heartbeat',
            status='UNKNOWN'
        )
        
        heartbeat_payload = {
            "mcp_version": "1.0",
            "message_id": str(uuid.uuid4()),
            "command": "heartbeat",
            "payload": {
                "name": "test-agent-mcp-rest",
                "timestamp": datetime.now().isoformat(),
                "status": "OK"
            }
        }
        
        response = self.client.post(
            '/api/mcp/heartbeat/',
            heartbeat_payload,
            format='json'
        )
        
        # Should return 200 OK
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Should return success status
        data = response.json()
        self.assertEqual(data.get('status'), 'success')
        
        # Should have updated the agent status
        test_agent.refresh_from_db()
        self.assertEqual(test_agent.status, 'OK')
        self.assertIsNotNone(test_agent.last_heartbeat)
    
    def test_mcp_invalid_request_format(self):
        """Test MCP endpoint with invalid request format."""
        invalid_payload = {
            "invalid_field": "test"
        }
        
        response = self.client.post(
            '/api/mcp/discover-capabilities/',
            invalid_payload,
            format='json'
        )
        
        # Should return 400 Bad Request for invalid format
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
    
    def test_mcp_missing_required_fields(self):
        """Test MCP endpoint with missing required fields."""
        incomplete_payload = {
            "mcp_version": "1.0",
            # Missing message_id and command
        }
        
        response = self.client.post(
            '/api/mcp/discover-capabilities/',
            incomplete_payload,
            format='json'
        )
        
        # Should return 400 Bad Request for missing fields
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
    
    def test_mcp_authenticated_endpoint_access(self):
        """Test that MCP endpoints work with authentication if required."""
        # Set token authentication
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        
        capabilities_payload = {
            "mcp_version": "1.0",
            "message_id": str(uuid.uuid4()),
            "command": "discover_capabilities",
            "payload": {}
        }
        
        response = self.client.post(
            '/api/mcp/discover-capabilities/',
            capabilities_payload,
            format='json'
        )
        
        # Should work with authentication
        self.assertEqual(response.status_code, status.HTTP_200_OK)
    
    def tearDown(self):
        """Clean up after tests."""
        # Clean up is handled automatically by Django's TestCase
        pass