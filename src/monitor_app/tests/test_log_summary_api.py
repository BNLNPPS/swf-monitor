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


class LogSummaryAPITests(TestCase):
    def setUp(self):
        # Use unique usernames for each test run
        self.username = f"testuser_{uuid.uuid4()}"
        self.user = User.objects.create_user(username=self.username, password="testpass")
        self.client.login(username=self.username, password="testpass")
        now = timezone.now()
        # Create logs for two apps and two instances
        AppLog.objects.create(app_name='app1', instance_name='inst1', timestamp=now, level=logging.ERROR, levelname='ERROR', message='Error 1', module='mod', funcname='f', lineno=1, process=1, thread=1)
        AppLog.objects.create(app_name='app1', instance_name='inst1', timestamp=now, level=logging.INFO, levelname='INFO', message='Info 1', module='mod', funcname='f', lineno=2, process=1, thread=1)
        AppLog.objects.create(app_name='app1', instance_name='inst2', timestamp=now, level=logging.ERROR, levelname='ERROR', message='Error 2', module='mod', funcname='f', lineno=3, process=1, thread=1)
        AppLog.objects.create(app_name='app2', instance_name='inst3', timestamp=now, level=logging.CRITICAL, levelname='CRITICAL', message='Critical 1', module='mod', funcname='f', lineno=4, process=1, thread=1)

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