from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from monitor_app.models import SystemAgent, AppLog, Run, StfFile, Subscriber
from monitor_app.serializers import AppLogSerializer
from django.core.management import call_command
from io import StringIO
import logging
import uuid
import re


class StfFileAPITests(APITestCase):
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

    def test_list_stf_files(self):
        url = reverse('monitor_app:stffile-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_create_stf_file(self):
        url = reverse('monitor_app:stffile-list')
        data = {
            'run': self.run.run_id,
            'machine_state': 'cosmics',
            'file_url': 'https://example.com/files/test2.stf',
            'file_size_bytes': 2048000,
            'checksum': 'def789abc123',
            'status': 'registered'
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(StfFile.objects.count(), 2)

    def test_get_stf_file(self):
        url = reverse('monitor_app:stffile-detail', kwargs={'pk': self.stf_file.file_id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['file_url'], "https://example.com/files/test.stf")

    def test_update_stf_file_status(self):
        url = reverse('monitor_app:stffile-detail', kwargs={'pk': self.stf_file.file_id})
        data = {'status': 'processing'}
        response = self.client.patch(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.stf_file.refresh_from_db()
        self.assertEqual(self.stf_file.status, 'processing')

    def test_delete_stf_file(self):
        url = reverse('monitor_app:stffile-detail', kwargs={'pk': self.stf_file.file_id})
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(StfFile.objects.filter(pk=self.stf_file.file_id).exists())

    def test_create_stf_file_duplicate_url(self):
        url = reverse('monitor_app:stffile-list')
        data = {
            'run': self.run.run_id,
            'file_url': 'https://example.com/files/test.stf',  # Same as existing
            'file_size_bytes': 1000,
            'checksum': 'duplicate'
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_status_value(self):
        url = reverse('monitor_app:stffile-detail', kwargs={'pk': self.stf_file.file_id})
        data = {'status': 'invalid_status'}
        response = self.client.patch(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthenticated_access_denied(self):
        self.client.force_authenticate(user=None)
        url = reverse('monitor_app:stffile-list')
        response = self.client.get(url)
        self.assertIn(response.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])