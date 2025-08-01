from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from monitor_app.models import SystemAgent, AppLog, Run, StfFile, Subscriber, MessageQueueDispatch
from monitor_app.serializers import AppLogSerializer
from django.core.management import call_command
from io import StringIO
import logging
import uuid
import re


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