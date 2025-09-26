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


class AppLogAPITests(APITestCase):
    def setUp(self):
        unique_username = f"testuser_{uuid.uuid4()}"
        self.user = User.objects.create_user(username=unique_username, password='testpassword')
        self.client.force_authenticate(user=self.user)
        self.url = reverse('monitor_app:applog-list')
        self.log_data = {
            'app_name': 'test_app',
            'instance_name': 'test_instance',
            'timestamp': timezone.now().isoformat(),
            'level': logging.INFO,
            'levelname': 'INFO',
            'message': 'This is a test log message.',
            'module': 'test_module',
            'funcname': 'test_func',
            'lineno': 123,
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