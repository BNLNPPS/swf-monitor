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


class RunAPITests(APITestCase):
    def setUp(self):
        unique_username = f"testuser_{uuid.uuid4()}"
        self.user = User.objects.create_user(username=unique_username, password='testpassword')
        self.client.force_authenticate(user=self.user)
        self.run = Run.objects.create(
            run_number=12345,
            start_time=timezone.now(),
            run_conditions={'beam_energy': 10.0, 'detector_config': 'standard'}
        )

    def test_list_runs(self):
        url = reverse('run-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_create_run(self):
        url = reverse('run-list')
        data = {
            'run_number': 12346,
            'start_time': timezone.now().isoformat(),
            'run_conditions': {'beam_energy': 12.0, 'detector_config': 'high_rate'}
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Run.objects.count(), 2)

    def test_get_run(self):
        url = reverse('run-detail', kwargs={'pk': self.run.run_id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['run_number'], 12345)

    def test_update_run(self):
        url = reverse('run-detail', kwargs={'pk': self.run.run_id})
        data = {'end_time': timezone.now().isoformat()}
        response = self.client.patch(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.run.refresh_from_db()
        self.assertIsNotNone(self.run.end_time)

    def test_delete_run(self):
        url = reverse('run-detail', kwargs={'pk': self.run.run_id})
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Run.objects.filter(pk=self.run.run_id).exists())

    def test_create_run_duplicate_number(self):
        url = reverse('run-list')
        data = {
            'run_number': 12345,  # Same as existing run
            'start_time': timezone.now().isoformat()
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthenticated_access_denied(self):
        self.client.force_authenticate(user=None)
        url = reverse('run-list')
        response = self.client.get(url)
        self.assertIn(response.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])