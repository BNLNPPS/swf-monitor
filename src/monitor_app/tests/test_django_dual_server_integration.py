#!/usr/bin/env python3
"""
Integration test for Django dual server configuration (Django HTTP on 8002, Django HTTPS on 8443).
This tests the actual running Django servers, not just Django's test framework.

Run this test while the Django dual server is running:
./start_django_dual.sh

Then run:
python manage.py test monitor_app.tests.test_django_dual_server_integration --keepdb
"""

import requests
import urllib3
import os
import sys
from datetime import datetime
from django.test import TestCase
from django.conf import settings

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class DjangoDualServerIntegrationTest(TestCase):
    """Integration tests for the actual running Django dual server configuration."""
    
    @classmethod
    def setUpClass(cls):
        """Set up for Django dual server integration tests."""
        super().setUpClass()
        
        # URLs for the actual running Django servers
        cls.django_http_url = os.getenv('SWF_MONITOR_HTTP_URL', 'http://localhost:8002')
        cls.django_https_url = os.getenv('SWF_MONITOR_URL', 'https://localhost:8443')
        cls.api_token = os.getenv('SWF_API_TOKEN', '')
        
        # Check if Django servers are running
        cls.django_http_running = cls._check_django_server_running(cls.django_http_url)
        cls.django_https_running = cls._check_django_server_running(cls.django_https_url, verify_ssl=False)
        
        if not cls.django_http_running:
            print(f"⚠️  Django HTTP server not running at {cls.django_http_url}")
        if not cls.django_https_running:
            print(f"⚠️  Django HTTPS server not running at {cls.django_https_url}")
    
    @classmethod
    def _check_django_server_running(cls, url, verify_ssl=True):
        """Check if a Django server is running at the given URL."""
        try:
            response = requests.get(f"{url}/admin/login/", 
                                  timeout=2, 
                                  verify=verify_ssl)
            return response.status_code in [200, 302, 404]  # Any valid HTTP response
        except:
            return False
    
    def test_django_http_server_logging_endpoint(self):
        """Test Django HTTP server (port 8002) for REST logging without authentication."""
        if not self.django_http_running:
            self.skipTest(f"Django HTTP server not running at {self.django_http_url}")
        
        log_data = {
            'app_name': 'django_integration_test',
            'instance_name': 'test_django_dual_server',
            'timestamp': datetime.now().isoformat(),
            'level': 20,  # INFO
            'levelname': 'INFO',
            'message': 'Integration test log via Django HTTP server',
            'module': 'test_django_dual_server_integration',
            'funcname': 'test_django_http_server_logging_endpoint',
            'lineno': 55,
            'process': os.getpid(),
            'thread': 0,
            'extra_data': {'test_type': 'integration', 'server': 'Django_HTTP'}
        }
        
        response = requests.post(
            f"{self.django_http_url}/api/logs/",
            json=log_data,
            timeout=5
        )
        
        # Should succeed without authentication
        self.assertEqual(response.status_code, 201, 
                        f"Django HTTP logging failed: {response.text}")
        
        # Verify response contains the created log
        response_data = response.json()
        self.assertEqual(response_data['app_name'], 'django_integration_test')
        self.assertEqual(response_data['message'], 'Integration test log via Django HTTP server')
    
    def test_django_https_server_unauthenticated_request(self):
        """Test Django HTTPS server (port 8443) returns 403 for unauthenticated requests."""
        if not self.django_https_running:
            self.skipTest(f"Django HTTPS server not running at {self.django_https_url}")
        
        response = requests.get(
            f"{self.django_https_url}/api/systemagents/",
            verify=False,  # Skip SSL verification for self-signed cert
            timeout=5
        )
        
        # Should return 403 Forbidden for unauthenticated request
        self.assertEqual(response.status_code, 403,
                        f"Expected 403 for unauthenticated Django HTTPS request, got {response.status_code}")
    
    def test_django_https_server_authenticated_request(self):
        """Test Django HTTPS server (port 8443) with proper authentication."""
        if not self.django_https_running:
            self.skipTest(f"Django HTTPS server not running at {self.django_https_url}")
        
        if not self.api_token:
            self.skipTest("No API token available in SWF_API_TOKEN environment variable")
        
        headers = {'Authorization': f'Token {self.api_token}'}
        
        response = requests.get(
            f"{self.django_https_url}/api/systemagents/",
            headers=headers,
            verify=False,  # Skip SSL verification for self-signed cert
            timeout=5
        )
        
        # Should return 200 OK with authentication
        self.assertEqual(response.status_code, 200,
                        f"Django HTTPS authenticated request failed: {response.text}")
        
        # Should return a list of agents
        data = response.json()
        self.assertIsInstance(data, list, "Response should be a list of agents")
    
    def test_django_https_server_heartbeat_endpoint(self):
        """Test Django HTTPS server heartbeat endpoint with authentication."""
        if not self.django_https_running:
            self.skipTest(f"Django HTTPS server not running at {self.django_https_url}")
        
        if not self.api_token:
            self.skipTest("No API token available in SWF_API_TOKEN environment variable")
        
        headers = {'Authorization': f'Token {self.api_token}'}
        heartbeat_data = {
            'instance_name': 'django_integration_test_agent',
            'agent_type': 'test',
            'status': 'OK',
            'last_heartbeat': datetime.now().isoformat()
        }
        
        response = requests.post(
            f"{self.django_https_url}/api/systemagents/heartbeat/",
            json=heartbeat_data,
            headers=headers,
            verify=False,
            timeout=5
        )
        
        # Should return 200 OK or 201 Created (depending on whether agent exists)
        self.assertIn(response.status_code, [200, 201],
                     f"Django HTTPS heartbeat failed: {response.text}")
        
        # Verify response
        response_data = response.json()
        self.assertEqual(response_data['instance_name'], 'django_integration_test_agent')
    
    def test_django_dual_server_configuration_summary(self):
        """Test that both Django servers are properly configured for their intended purposes."""
        results = {
            'django_http_server': {
                'url': self.django_http_url,
                'running': self.django_http_running,
                'purpose': 'Django REST logging (no auth required)'
            },
            'django_https_server': {
                'url': self.django_https_url,
                'running': self.django_https_running,
                'purpose': 'Django authenticated API calls'
            }
        }
        
        # Print configuration summary
        print("\n" + "="*60)
        print("DJANGO DUAL SERVER CONFIGURATION TEST SUMMARY")
        print("="*60)
        
        for server_type, config in results.items():
            status = "✅ RUNNING" if config['running'] else "❌ NOT RUNNING"
            print(f"{server_type.upper()}: {status}")
            print(f"  URL: {config['url']}")
            print(f"  Purpose: {config['purpose']}")
        
        print("="*60)
        
        # Both Django servers should be running for full functionality
        if self.django_http_running and self.django_https_running:
            print("✅ DJANGO DUAL SERVER CONFIGURATION: FULLY OPERATIONAL")
        else:
            print("⚠️  DJANGO DUAL SERVER CONFIGURATION: INCOMPLETE")
            if not self.django_http_running and not self.django_https_running:
                self.fail("Neither Django HTTP nor Django HTTPS server is running. Start with: ./start_django_dual.sh")
            elif not self.django_http_running:
                self.fail(f"Django HTTP server not running at {self.django_http_url}")
            elif not self.django_https_running:
                self.fail(f"Django HTTPS server not running at {self.django_https_url}")
        
        print("")
    
    @classmethod
    def tearDownClass(cls):
        """Clean up after Django dual server integration tests."""
        super().tearDownClass()
        # Integration tests don't need special cleanup
        pass