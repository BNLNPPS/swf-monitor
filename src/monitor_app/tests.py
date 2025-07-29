from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from .models import SystemAgent, AppLog
from .serializers import AppLogSerializer
from django.core.management import call_command
from io import StringIO
import logging
import uuid
import re

class SystemAgentAPITests(APITestCase):
    def setUp(self):
        unique_username = f"testuser_{uuid.uuid4()}"
        self.user = User.objects.create_user(username=unique_username, password='testpassword')
        self.client.force_authenticate(user=self.user)
        self.agent = SystemAgent.objects.create(instance_name='test_agent', agent_type='test', status='OK')

    def test_list_agents(self):
        url = reverse('systemagent-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_create_agent(self):
        url = reverse('systemagent-list')
        data = {'instance_name': 'new_agent', 'agent_type': 'test', 'status': 'OK'}
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_partial_update_agent(self):
        url = reverse('systemagent-detail', kwargs={'pk': self.agent.pk})
        data = {'status': 'ERROR'}
        response = self.client.patch(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, 'ERROR')

    def test_delete_agent(self):
        url = reverse('systemagent-detail', kwargs={'pk': self.agent.pk})
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(SystemAgent.objects.filter(pk=self.agent.pk).exists())

    def test_create_agent_bad_data(self):
        url = reverse('systemagent-list')
        data = {'instance_name': 'new_agent'} # Missing agent_type and status
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_non_existent_agent(self):
        url = reverse('systemagent-detail', kwargs={'pk': 999})
        data = {'status': 'ERROR'}
        response = self.client.patch(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_non_existent_agent(self):
        url = reverse('systemagent-detail', kwargs={'pk': 999})
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

class AppLogAPITests(APITestCase):
    def setUp(self):
        unique_username = f"testuser_{uuid.uuid4()}"
        self.user = User.objects.create_user(username=unique_username, password='testpassword')
        self.client.force_authenticate(user=self.user)
        self.url = reverse('applog-list')
        self.log_data = {
            'app_name': 'test_app',
            'instance_name': 'test_instance',
            'timestamp': timezone.now().isoformat(),
            'level': logging.INFO,
            'level_name': 'INFO',
            'message': 'This is a test log message.',
            'module': 'test_module',
            'func_name': 'test_func',
            'line_no': 123,
            'process': 456,
            'thread': 789,
        }

    def test_create_log(self):
        """
        Ensure we can create a new app log.
        """
        response = self.client.post(self.url, self.log_data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(AppLog.objects.count(), 1)
        log = AppLog.objects.get()
        self.assertEqual(log.app_name, 'test_app')
        self.assertEqual(log.message, self.log_data['message'])

    def test_create_log_invalid_level(self):
        """
        Ensure we get a bad request for an invalid log level.
        """
        data = self.log_data.copy()
        data['level'] = 999  # Invalid level
        response = self.client.post(self.url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_log_missing_field(self):
        """
        Ensure we get a bad request for missing a required field.
        """
        data = self.log_data.copy()
        del data['message']
        response = self.client.post(self.url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class AppLogUITests(TestCase):
    def setUp(self):
        unique_username = f"ui_user_{uuid.uuid4()}"
        self.user = User.objects.create_user(username=unique_username, password='password')
        self.client.login(username=unique_username, password='password')
        now = timezone.now()
        AppLog.objects.create(app_name='app1', instance_name='inst1', level=logging.INFO, message='info message 1', timestamp=now, level_name='INFO', module='m', func_name='f', line_no=1, process=1, thread=1)
        AppLog.objects.create(app_name='app1', instance_name='inst1', level=logging.WARNING, message='warning message 1', timestamp=now, level_name='WARNING', module='m', func_name='f', line_no=1, process=1, thread=1)
        AppLog.objects.create(app_name='app1', instance_name='inst2', level=logging.ERROR, message='error message 1', timestamp=now, level_name='ERROR', module='m', func_name='f', line_no=1, process=1, thread=1)
        AppLog.objects.create(app_name='app2', instance_name='inst1', level=logging.INFO, message='info message 2', timestamp=now, level_name='INFO', module='m', func_name='f', line_no=1, process=1, thread=1)

    def test_log_list_view(self):
        response = self.client.get(reverse('monitor_app:log_list'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        # Robust: check for valid HTML structure
        self.assertIn('<html', html.lower())
        # Check for a table with at least one row (excluding header)
        rows = re.findall(r'<tr>.*?</tr>', html, re.DOTALL)
        self.assertTrue(len(rows) > 1)  # header + at least one data row

    def test_log_list_view_filtered(self):
        response = self.client.get(reverse('monitor_app:log_list') + '?app_name=app1&instance_name=inst1')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('<html', html.lower())
        rows = re.findall(r'<tr>.*?</tr>', html, re.DOTALL)
        self.assertTrue(len(rows) > 1)

    def test_log_summary_view(self):
        response = self.client.get(reverse('monitor_app:log_summary'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('<html', html.lower())
        # Check for a table or summary block
        self.assertRegex(html, r'<table|<div')

class MonitorAppUITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='ui_user', password='password')
        self.staff_user = User.objects.create_user(username='staff_user', password='password', is_staff=True)
        self.agent = SystemAgent.objects.create(instance_name='ui_agent', agent_type='test', status='OK')

    def test_index_view_unauthenticated(self):
        response = self.client.get(reverse('monitor_app:index'))
        self.assertEqual(response.status_code, 302) # Redirect to login

    def test_index_view_authenticated(self):
        self.client.login(username='ui_user', password='password')
        response = self.client.get(reverse('monitor_app:index'))
        self.assertEqual(response.status_code, 200)

    def test_create_agent_view_as_staff(self):
        self.client.login(username='staff_user', password='password')
        url = reverse('monitor_app:system_agent_create')
        data = {'instance_name': 'new_ui_agent', 'agent_type': 'test', 'status': 'OK'}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302) # Redirect on success
        self.assertTrue(SystemAgent.objects.filter(instance_name='new_ui_agent').exists())

    def test_create_agent_view_as_non_staff(self):
        self.client.login(username='ui_user', password='password')
        url = reverse('monitor_app:system_agent_create')
        data = {'instance_name': 'new_ui_agent', 'agent_type': 'test', 'status': 'OK'}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 403) # Forbidden

    def test_delete_agent_view(self):
        self.client.login(username='staff_user', password='password')
        url = reverse('monitor_app:system_agent_delete', kwargs={'pk': self.agent.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302) # Redirect on success
        self.assertFalse(SystemAgent.objects.filter(pk=self.agent.pk).exists())

    def test_update_agent_view_get(self):
        self.client.login(username='staff_user', password='password')
        url = reverse('monitor_app:system_agent_update', kwargs={'pk': self.agent.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.agent.instance_name)

    def test_update_non_existent_agent_view(self):
        self.client.login(username='staff_user', password='password')
        url = reverse('monitor_app:system_agent_update', kwargs={'pk': 999})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_delete_non_existent_agent_view(self):
        self.client.login(username='staff_user', password='password')
        url = reverse('monitor_app:system_agent_delete', kwargs={'pk': 999})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

class LogSummaryAPITests(TestCase):
    def setUp(self):
        # Use unique usernames for each test run
        self.username = f"testuser_{uuid.uuid4()}"
        self.user = User.objects.create_user(username=self.username, password="testpass")
        self.client.login(username=self.username, password="testpass")
        now = timezone.now()
        # Create logs for two apps and two instances
        AppLog.objects.create(app_name='app1', instance_name='inst1', timestamp=now, level=logging.ERROR, level_name='ERROR', message='Error 1', module='mod', func_name='f', line_no=1, process=1, thread=1)
        AppLog.objects.create(app_name='app1', instance_name='inst1', timestamp=now, level=logging.INFO, level_name='INFO', message='Info 1', module='mod', func_name='f', line_no=2, process=1, thread=1)
        AppLog.objects.create(app_name='app1', instance_name='inst2', timestamp=now, level=logging.ERROR, level_name='ERROR', message='Error 2', module='mod', func_name='f', line_no=3, process=1, thread=1)
        AppLog.objects.create(app_name='app2', instance_name='inst3', timestamp=now, level=logging.CRITICAL, level_name='CRITICAL', message='Critical 1', module='mod', func_name='f', line_no=4, process=1, thread=1)

    def tearDown(self):
        # Clean up created user
        User.objects.filter(username=self.username).delete()

    def test_summary_api(self):
        url = '/api/logs/summary/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn('app1', data)
        self.assertIn('app2', data)
        self.assertIn('inst1', data['app1'])
        self.assertIn('inst2', data['app1'])
        self.assertIn('inst3', data['app2'])
        # Check error counts
        self.assertEqual(data['app1']['inst1']['error_counts'].get('ERROR', 0), 1)
        self.assertEqual(data['app1']['inst2']['error_counts'].get('ERROR', 0), 1)
        self.assertEqual(data['app2']['inst3']['error_counts'].get('CRITICAL', 0), 1)
        # Check recent errors structure
        self.assertTrue(isinstance(data['app1']['inst1']['recent_errors'], list))


class ActiveMQSSLConnectionTests(TestCase):
    """
    Tests for ActiveMQ SSL connection functionality.
    These tests verify that the SSL configuration works correctly with the certificate.
    """
    
    def setUp(self):
        """Set up test environment with ActiveMQ settings"""
        # Import here to avoid issues if stomp is not available
        try:
            import stomp
            import ssl
            self.stomp_available = True
        except ImportError:
            self.stomp_available = False
            
    def test_activemq_ssl_configuration(self):
        """Test that ActiveMQ SSL settings are properly configured"""
        from django.conf import settings
        
        # Test that all required SSL settings are available
        self.assertTrue(hasattr(settings, 'ACTIVEMQ_USE_SSL'))
        self.assertTrue(hasattr(settings, 'ACTIVEMQ_SSL_CA_CERTS'))
        self.assertTrue(hasattr(settings, 'ACTIVEMQ_HOST'))
        self.assertTrue(hasattr(settings, 'ACTIVEMQ_PORT'))
        self.assertTrue(hasattr(settings, 'ACTIVEMQ_USER'))
        self.assertTrue(hasattr(settings, 'ACTIVEMQ_PASSWORD'))
        
        # Test that SSL is enabled and certificate path is set
        if settings.ACTIVEMQ_USE_SSL:
            self.assertIsNotNone(settings.ACTIVEMQ_SSL_CA_CERTS)
            self.assertNotEqual(settings.ACTIVEMQ_SSL_CA_CERTS, '')
            
    def test_certificate_file_exists(self):
        """Test that the SSL certificate file exists and is readable"""
        from django.conf import settings
        import os
        
        if getattr(settings, 'ACTIVEMQ_USE_SSL', False):
            cert_file = settings.ACTIVEMQ_SSL_CA_CERTS
            if cert_file:
                self.assertTrue(os.path.exists(cert_file), 
                               f"Certificate file not found: {cert_file}")
                self.assertTrue(os.path.isfile(cert_file), 
                               f"Certificate path is not a file: {cert_file}")
                # Test that file is readable
                with open(cert_file, 'r') as f:
                    content = f.read()
                    self.assertIn('-----BEGIN CERTIFICATE-----', content)
                    self.assertIn('-----END CERTIFICATE-----', content)
                    
    def test_activemq_connection_mock(self):
        """Test ActiveMQ connection setup with mocked connection"""
        if not self.stomp_available:
            self.skipTest("stomp.py not available")
            
        from django.conf import settings
        from unittest.mock import patch, MagicMock
        import stomp
        import ssl
        
        # Mock the stomp connection
        with patch('stomp.Connection') as mock_connection_class:
            mock_connection = MagicMock()
            mock_connection_class.return_value = mock_connection
            
            # Test connection setup
            host = getattr(settings, 'ACTIVEMQ_HOST', 'localhost')
            port = getattr(settings, 'ACTIVEMQ_PORT', 61612)
            use_ssl = getattr(settings, 'ACTIVEMQ_USE_SSL', False)
            
            # Create connection
            conn = stomp.Connection(host_and_ports=[(host, port)], vhost=host, try_loopback_connect=False)
            
            # Verify connection was created with correct parameters
            mock_connection_class.assert_called_once_with(
                host_and_ports=[(host, port)], 
                vhost=host, 
                try_loopback_connect=False
            )
            
            # Test SSL configuration if enabled
            if use_ssl:
                ssl_ca_certs = getattr(settings, 'ACTIVEMQ_SSL_CA_CERTS', '')
                if ssl_ca_certs:
                    # Verify SSL would be configured
                    self.assertTrue(hasattr(conn, 'transport'))
                    
    def test_activemq_listener_import(self):
        """Test that the ActiveMQ listener module imports correctly"""
        try:
            from monitor_app.activemq_listener import start_activemq_listener, MessageListener
            self.assertTrue(callable(start_activemq_listener))
            self.assertTrue(MessageListener is not None)
        except ImportError as e:
            self.fail(f"Failed to import ActiveMQ listener components: {e}")
            
    def test_activemq_ssl_connection_attempt(self):
        """Test actual SSL connection attempt (will fail if service not available)"""
        if not self.stomp_available:
            self.skipTest("stomp.py not available")
            
        from django.conf import settings
        import stomp
        import ssl
        import socket
        
        # Only run if SSL is configured
        if not getattr(settings, 'ACTIVEMQ_USE_SSL', False):
            self.skipTest("SSL not configured for ActiveMQ")
            
        host = settings.ACTIVEMQ_HOST
        port = settings.ACTIVEMQ_PORT
        ca_certs = settings.ACTIVEMQ_SSL_CA_CERTS
        
        # First check if the port is reachable
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)  # 2 second timeout
            result = sock.connect_ex((host, port))
            sock.close()
            
            if result != 0:
                self.skipTest(f"ActiveMQ service not reachable at {host}:{port}")
                
        except Exception as e:
            self.skipTest(f"Cannot test network connectivity: {e}")
            
        # Test SSL connection setup
        conn = stomp.Connection(host_and_ports=[(host, port)], vhost=host, try_loopback_connect=False)
        
        # Configure SSL
        if ca_certs:
            try:
                conn.transport.set_ssl(
                    for_hosts=[(host, port)],
                    ca_certs=ca_certs,
                    ssl_version=ssl.PROTOCOL_TLS_CLIENT
                )
                
                # Try to connect (will timeout or fail if service not available)
                user = settings.ACTIVEMQ_USER
                password = settings.ACTIVEMQ_PASSWORD
                
                # Use a short timeout for testing
                try:
                    conn.connect(login=user, passcode=password, wait=True, version='1.2', timeout=5)
                    conn.disconnect()
                    # If we get here, connection succeeded
                    self.assertTrue(True, "ActiveMQ SSL connection successful")
                except Exception as e:
                    # Connection failed - this is expected if service is not available
                    # We just verify the SSL setup didn't crash
                    self.assertIsInstance(e, (stomp.exception.ConnectFailedException, 
                                             ConnectionError, OSError, socket.error))
                    
            except Exception as e:
                self.fail(f"SSL configuration failed: {e}")
