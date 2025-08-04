"""
Integration tests for agent-monitor communication.
Uses the exact same approach as the working test script.
"""

import os
import time
import requests
from pathlib import Path

from django.test import TestCase
import urllib3

# Disable SSL warnings for self-signed certificates in tests
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class TestAgentMonitorIntegration(TestCase):
    """Test integration between agents and monitor API using exact working approach."""
    
    def setUp(self):
        """Set up test environment exactly like working agents."""
        # Load environment exactly like working agents do
        env_file = Path.home() / ".env"
        if env_file.exists():
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        if line.startswith('export '):
                            line = line[7:]  # Remove 'export '
                        key, value = line.split('=', 1)
                        os.environ[key] = value.strip('"\'')
        
        # Unset proxy variables exactly like working agents
        for proxy_var in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY']:
            if proxy_var in os.environ:
                del os.environ[proxy_var]
        
        # Get configuration exactly like working agents
        self.monitor_url = os.getenv('SWF_MONITOR_URL', 'https://localhost:8443')
        self.api_token = os.getenv('SWF_API_TOKEN')
        
        if not self.api_token:
            self.skipTest("SWF_API_TOKEN environment variable not set")
        
        # Configure session exactly like working agents
        self.session = requests.Session()
        self.session.headers.update({'Authorization': f'Token {self.api_token}'})
        self.session.verify = False  # Allow self-signed certs
        self.session.proxies = {'http': None, 'https': None}
    
    def test_agent_heartbeat_exactly_like_working_script(self):
        """Test heartbeat using the exact same approach as the working script."""
        # Use exact same heartbeat data as working script
        heartbeat_data = {
            'instance_name': 'django-test-agent',
            'agent_type': 'TEST', 
            'status': 'OK',
            'description': 'Django test using exact working approach',
            'mq_connected': False
        }
        
        try:
            # Make exact same API call as working script
            response = self.session.post(
                f"{self.monitor_url}/api/systemagents/heartbeat/",
                json=heartbeat_data,
                timeout=10
            )
            
            # Check results exactly like working script
            print(f"Response status: {response.status_code}")
            print(f"Response text: {response.text}")
            
            if response.status_code in [200, 201]:
                result = response.json()
                self.assertIn('instance_name', result)
                self.assertEqual(result['instance_name'], 'django-test-agent')
                print("✅ Django test SUCCESS: Heartbeat sent successfully!")
            else:
                self.fail(f"Unexpected response {response.status_code}: {response.text}")
                
        except requests.exceptions.ConnectionError as e:
            self.skipTest(f"Monitor not running: {e}")
        except Exception as e:
            self.fail(f"Test error: {e}")
    
    def test_agent_list_api_exactly_like_working_approach(self):
        """Test system agents list using exact working approach."""
        try:
            response = self.session.get(
                f"{self.monitor_url}/api/systemagents/",
                timeout=10
            )
            
            print(f"List agents response status: {response.status_code}")
            
            if response.status_code == 200:
                agents = response.json()
                self.assertIsInstance(agents, list)
                print(f"✅ Found {len(agents)} agents in monitor")
                
                # Look for our test agents
                test_agents = [a for a in agents if 'test' in a['instance_name'].lower()]
                if test_agents:
                    print(f"Found test agents: {[a['instance_name'] for a in test_agents]}")
            else:
                self.fail(f"Failed to get agents list: {response.status_code}")
                
        except requests.exceptions.ConnectionError as e:
            self.skipTest(f"Monitor not running: {e}")
        except Exception as e:
            self.fail(f"Test error: {e}")


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "-s"])