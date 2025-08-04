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


class MessageQueueDispatchAPITests(APITestCase):
    def setUp(self):
        unique_username = f"testuser_{uuid.uuid4()}"
        self.user = User.objects.create_user(username=unique_username, password='testpassword')
        self.client.force_authenticate(user=self.user)
        self.run = Run.objects.create(
            run_number=12345,
            start_time=timezone.now()
        )
        self.stf_file = StfFile.objects.create(
            run=self.run,
            machine_state="physics",
            file_url="https://example.com/files/test.stf",
            file_size_bytes=1024000,
            checksum="abc123def456"
        )
        self.dispatch = MessageQueueDispatch.objects.create(
            stf_file=self.stf_file,
            message_content={"file_path": "/data/test.stf", "status": "ready"},
            is_successful=True
        )

    def test_list_dispatches(self):
        url = reverse('monitor_app:messagedispatch-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_create_dispatch(self):
        url = reverse('monitor_app:messagedispatch-list')
        data = {
            'stf_file': str(self.stf_file.file_id),
            'message_content': {"file_path": "/data/test2.stf", "status": "processing"},
            'is_successful': False,
            'error_message': 'Queue connection failed'
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(MessageQueueDispatch.objects.count(), 2)

    def test_get_dispatch(self):
        url = reverse('monitor_app:messagedispatch-detail', kwargs={'pk': self.dispatch.dispatch_id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['is_successful'])

    def test_update_dispatch_status(self):
        url = reverse('monitor_app:messagedispatch-detail', kwargs={'pk': self.dispatch.dispatch_id})
        data = {
            'is_successful': False,
            'error_message': 'Updated: Connection timeout'
        }
        response = self.client.patch(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.dispatch.refresh_from_db()
        self.assertFalse(self.dispatch.is_successful)
        self.assertEqual(self.dispatch.error_message, 'Updated: Connection timeout')

    def test_delete_dispatch(self):
        url = reverse('monitor_app:messagedispatch-detail', kwargs={'pk': self.dispatch.dispatch_id})
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(MessageQueueDispatch.objects.filter(pk=self.dispatch.dispatch_id).exists())

    def test_create_dispatch_invalid_stf_file(self):
        url = reverse('monitor_app:messagedispatch-list')
        data = {
            'stf_file': '00000000-0000-0000-0000-000000000000',  # Non-existent UUID
            'message_content': {"test": "data"},
            'is_successful': True
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_dispatch_time_auto_set(self):
        """Test that dispatch_time is automatically set on creation"""
        url = reverse('monitor_app:messagedispatch-list')
        before_creation = timezone.now()
        data = {
            'stf_file': str(self.stf_file.file_id),
            'message_content': {"test": "auto_time"},
            'is_successful': True
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        dispatch = MessageQueueDispatch.objects.get(pk=response.data['dispatch_id'])
        self.assertGreaterEqual(dispatch.dispatch_time, before_creation)

    def test_unauthenticated_access_denied(self):
        self.client.force_authenticate(user=None)
        url = reverse('monitor_app:messagedispatch-list')
        response = self.client.get(url)
        self.assertIn(response.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])