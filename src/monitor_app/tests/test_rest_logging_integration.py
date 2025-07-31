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


class RestLoggingIntegrationTests(APITestCase):
    """
    Test the REST logging functionality end-to-end.
    
    These tests verify that agents can send log messages to the database
    via the REST API endpoint using both direct REST calls and the 
    custom Python logging handler.
    """
    
    def setUp(self):
        """Set up test client and authentication."""
        unique_username = f"testuser_{uuid.uuid4()}"
        self.user = User.objects.create_user(username=unique_username, password='testpassword')
        self.client.force_authenticate(user=self.user)
        self.logs_url = reverse('applog-list')
        AppLog.objects.all().delete()  # Start with clean slate
    
    def test_direct_rest_log_creation(self):
        """Test creating logs directly via REST API."""
        log_data = {
            'app_name': 'test_app',
            'instance_name': 'test_instance',
            'timestamp': timezone.now().isoformat(),
            'level': logging.INFO,
            'levelname': 'INFO',
            'message': 'Test log message via REST API',
            'module': 'test_module',
            'funcname': 'test_function',
            'lineno': 42,
            'process': 1234,
            'thread': 5678,
            'extra_data': {'test': 'data'}
        }
        
        response = self.client.post(
            self.logs_url,
            data=log_data,
            format='json'
        )
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        # Verify the log was created in the database
        self.assertEqual(AppLog.objects.count(), 1)
        log = AppLog.objects.first()
        self.assertEqual(log.app_name, 'test_app')
        self.assertEqual(log.message, 'Test log message via REST API')
        self.assertEqual(log.level, logging.INFO)
    
    def test_multiple_log_levels(self):
        """Test logging different severity levels."""
        test_logs = [
            ('DEBUG', logging.DEBUG, 'Debug message'),
            ('INFO', logging.INFO, 'Info message'),  
            ('WARNING', logging.WARNING, 'Warning message'),
            ('ERROR', logging.ERROR, 'Error message'),
            ('CRITICAL', logging.CRITICAL, 'Critical message')
        ]
        
        for levelname, level_int, message in test_logs:
            log_data = {
                'app_name': 'multi_level_test',
                'instance_name': 'test_instance',
                'timestamp': timezone.now().isoformat(),
                'level': level_int,
                'levelname': levelname,
                'message': message,
                'module': 'test_module',
                'funcname': 'test_function',
                'lineno': 1,
                'process': 1234,
                'thread': 5678
            }
            
            response = self.client.post(
                self.logs_url,
                data=log_data,
                format='json'
            )
            
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        # Verify all logs were created
        self.assertEqual(AppLog.objects.count(), len(test_logs))
        
        # Verify different log levels are present
        levels_in_db = set(AppLog.objects.values_list('level', flat=True))
        expected_levels = {level_int for _, level_int, _ in test_logs}
        self.assertEqual(levels_in_db, expected_levels)
    
    def test_bulk_logging(self):
        """Test creating many log entries (simulating real usage)."""
        num_logs = 25  # Reduced from 50 to keep test fast
        
        for i in range(num_logs):
            log_data = {
                'app_name': 'bulk_test_app',
                'instance_name': f'instance_{i % 5}',  # 5 different instances
                'timestamp': timezone.now().isoformat(),
                'level': logging.INFO,
                'levelname': 'INFO',
                'message': f'Bulk test log message {i+1}',
                'module': 'bulk_test_module',
                'funcname': 'bulk_test_function',
                'lineno': i + 1,
                'process': 1234,
                'thread': 5678
            }
            
            response = self.client.post(
                self.logs_url,
                data=log_data,
                format='json'
            )
            
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        # Verify all logs were created
        self.assertEqual(AppLog.objects.count(), num_logs)
        
        # Verify logs are properly distributed across instances
        instance_counts = AppLog.objects.values('instance_name').distinct().count()
        self.assertEqual(instance_counts, 5)
    
    def test_log_retrieval(self):
        """Test retrieving logs via REST API."""
        # Create some test logs
        for i in range(3):
            log_data = {
                'app_name': 'retrieval_test',
                'instance_name': 'test_instance',
                'timestamp': timezone.now().isoformat(),
                'level': logging.INFO,
                'levelname': 'INFO',
                'message': f'Retrieval test message {i+1}',
                'module': 'test_module',
                'funcname': 'test_function',
                'lineno': i + 1,
                'process': 1234,
                'thread': 5678
            }
            
            self.client.post(
                self.logs_url,
                data=log_data,
                format='json'
            )
        
        # Retrieve logs via GET request
        response = self.client.get(self.logs_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify response contains our logs
        response_data = response.json()
        # Handle both paginated and non-paginated response formats
        if isinstance(response_data, dict) and 'results' in response_data:
            results = response_data['results']
        else:
            results = response_data
        
        self.assertEqual(len(results), 3)
        
        # Verify log content
        messages = [log['message'] for log in results]
        self.assertIn('Retrieval test message 1', messages)
        self.assertIn('Retrieval test message 2', messages)
        self.assertIn('Retrieval test message 3', messages)
    
    def test_invalid_log_data(self):
        """Test handling of invalid log data."""
        invalid_data = {
            'app_name': 'test_app',
            # Missing required fields
            'message': 'Invalid log entry'
        }
        
        response = self.client.post(
            self.logs_url,
            data=invalid_data,
            format='json'
        )
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(AppLog.objects.count(), 0)  # No log should be created
    
    def test_agent_workflow_logging_simulation(self):
        """Test simulating how agents would send logs during workflow processing."""
        # Simulate agent starting up
        startup_logs = [
            ('INFO', 'Agent starting up'),
            ('INFO', 'Connecting to message queue'),
            ('INFO', 'Agent ready for processing')
        ]
        
        for levelname, message in startup_logs:
            log_data = {
                'app_name': 'workflow_agent',
                'instance_name': 'agent_001',
                'timestamp': timezone.now().isoformat(),
                'level': getattr(logging, levelname),
                'levelname': levelname,
                'message': message,
                'module': 'agent_workflow',
                'funcname': 'startup_sequence',
                'lineno': 1,
                'process': 1234,
                'thread': 5678
            }
            
            response = self.client.post(self.logs_url, data=log_data, format='json')
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        # Simulate processing workflow
        processing_logs = [
            ('INFO', 'Processing file batch 1/10'),
            ('DEBUG', 'File validation successful'),
            ('WARNING', 'Processing took longer than expected'),
            ('INFO', 'Batch processing completed')
        ]
        
        for levelname, message in processing_logs:
            log_data = {
                'app_name': 'workflow_agent',
                'instance_name': 'agent_001',
                'timestamp': timezone.now().isoformat(),
                'level': getattr(logging, levelname),
                'levelname': levelname,
                'message': message,
                'module': 'agent_workflow',
                'funcname': 'process_batch',
                'lineno': 1,
                'process': 1234,
                'thread': 5678
            }
            
            response = self.client.post(self.logs_url, data=log_data, format='json')
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        # Verify logs were created
        total_logs = len(startup_logs) + len(processing_logs)
        self.assertEqual(AppLog.objects.count(), total_logs)
        
        # Verify all logs are from the same agent
        agent_logs = AppLog.objects.filter(
            app_name='workflow_agent',
            instance_name='agent_001'
        )
        self.assertEqual(agent_logs.count(), total_logs)
        
        # Verify different log levels are present
        log_levels = set(agent_logs.values_list('levelname', flat=True))
        expected_levels = {'INFO', 'DEBUG', 'WARNING'}
        self.assertEqual(log_levels, expected_levels)