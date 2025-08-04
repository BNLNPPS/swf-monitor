"""
Test Django HTTPS authentication for the dual server configuration.
This ensures that Django HTTPS endpoints properly require and validate authentication.
"""

from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework.authtoken.models import Token
from rest_framework import status
from monitor_app.models import SystemAgent
import urllib3

# Disable SSL warnings for test environment
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class DjangoHTTPSAuthenticationTest(TestCase):
    """Test Django HTTPS endpoints with proper authentication."""
    
    def setUp(self):
        """Set up test environment with user and token."""
        # Create test user
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        # Create API token for the user
        self.token = Token.objects.create(user=self.user)
        
        # Create test client
        self.client = APIClient()
        
        # Create test data
        self.agent = SystemAgent.objects.create(
            instance_name='test-agent-1',
            agent_type='test',
            description='Test agent for Django HTTPS authentication',
            status='OK'
        )
    
    def test_django_https_unauthenticated_request_returns_403(self):
        """Test that unauthenticated Django HTTPS requests return 403 Forbidden."""
        # Don't set any authentication
        response = self.client.get('/api/systemagents/')
        
        # Should return 403 due to DjangoModelPermissionsOrAnonReadOnly
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
    
    def test_django_https_authenticated_request_with_token(self):
        """Test that authenticated Django HTTPS requests with token work correctly."""
        # Set token authentication
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        
        # Make authenticated request
        response = self.client.get('/api/systemagents/')
        
        # Should return 200 OK
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Should return our test agent
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['instance_name'], 'test-agent-1')
    
    def test_django_https_authenticated_request_with_session(self):
        """Test that authenticated Django HTTPS requests with session work correctly."""
        # Login with session authentication
        self.client.login(username='testuser', password='testpass123')
        
        # Make authenticated request
        response = self.client.get('/api/systemagents/')
        
        # Should return 200 OK
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Should return our test agent
        data = response.json()
        self.assertEqual(len(data), 1)
    
    def test_django_https_invalid_token_returns_403(self):
        """Test that invalid token on Django HTTPS returns 403 Forbidden (due to DjangoModelPermissionsOrAnonReadOnly)."""
        # Set invalid token
        self.client.credentials(HTTP_AUTHORIZATION='Token invalid-token-12345')
        
        # Make request with bad token
        response = self.client.get('/api/systemagents/')
        
        # Should return 403 Forbidden (due to Django's permission system behavior)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
    
    def test_django_https_create_agent_with_authentication(self):
        """Test creating a system agent via Django HTTPS with authentication."""
        # Set token authentication
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        
        # Create new agent data
        new_agent_data = {
            'instance_name': 'test-agent-2',
            'agent_type': 'test',
            'description': 'Created via Django HTTPS test',
            'status': 'OK'
        }
        
        # POST to create new agent
        response = self.client.post('/api/systemagents/', new_agent_data, format='json')
        
        # Should return 201 Created
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        # Verify agent was created
        self.assertEqual(SystemAgent.objects.count(), 2)
        created_agent = SystemAgent.objects.get(instance_name='test-agent-2')
        self.assertEqual(created_agent.description, 'Created via Django HTTPS test')
    
    def test_django_https_heartbeat_endpoint_with_authentication(self):
        """Test the Django HTTPS heartbeat endpoint requires authentication."""
        # Test without authentication
        response = self.client.post('/api/systemagents/heartbeat/', {
            'instance_name': 'test-agent-1',
            'status': 'OK'
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        
        # Test with authentication
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        response = self.client.post('/api/systemagents/heartbeat/', {
            'instance_name': 'test-agent-1',
            'status': 'OK'
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
    
    def test_django_http_logs_endpoint_allows_anonymous(self):
        """Test that Django HTTP logs endpoint allows anonymous access (for HTTP logging on port 8002)."""
        # No authentication
        log_data = {
            'app_name': 'test_app',
            'instance_name': 'test_instance',
            'timestamp': '2025-08-04T10:00:00',
            'level': 20,
            'levelname': 'INFO',
            'message': 'Test log message via Django HTTP',
            'module': 'test_module',
            'funcname': 'test_func',
            'lineno': 100,
            'process': 1234,
            'thread': 5678
        }
        
        response = self.client.post('/api/logs/', log_data, format='json')
        
        # Should return 201 Created without authentication
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
    
    def tearDown(self):
        """Clean up after tests."""
        # Clean up is handled automatically by Django's TestCase
        pass