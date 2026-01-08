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


class AppLogUITests(TestCase):
    def setUp(self):
        unique_username = f"ui_user_{uuid.uuid4()}"
        self.user = User.objects.create_user(username=unique_username, password='password')
        self.client.login(username=unique_username, password='password')
        now = timezone.now()
        AppLog.objects.create(app_name='app1', instance_name='inst1', level=logging.INFO, message='info message 1', timestamp=now, levelname='INFO', module='m', funcname='f', lineno=1, process=1, thread=1)
        AppLog.objects.create(app_name='app1', instance_name='inst1', level=logging.WARNING, message='warning message 1', timestamp=now, levelname='WARNING', module='m', funcname='f', lineno=1, process=1, thread=1)
        AppLog.objects.create(app_name='app1', instance_name='inst2', level=logging.ERROR, message='error message 1', timestamp=now, levelname='ERROR', module='m', funcname='f', lineno=1, process=1, thread=1)
        AppLog.objects.create(app_name='app2', instance_name='inst1', level=logging.INFO, message='info message 2', timestamp=now, levelname='INFO', module='m', funcname='f', lineno=1, process=1, thread=1)

    def test_log_list_view(self):
        response = self.client.get(reverse('monitor_app:log_list'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        # Check for valid HTML structure
        self.assertIn('<html', html.lower())
        # DataTables template - check for table element (data loaded via AJAX)
        self.assertIn('<table', html.lower())

    def test_log_list_view_filtered(self):
        response = self.client.get(reverse('monitor_app:log_list') + '?app_name=app1&instance_name=inst1')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('<html', html.lower())
        # DataTables template - check for table element (data loaded via AJAX)
        self.assertIn('<table', html.lower())

    def test_log_summary_view(self):
        response = self.client.get(reverse('monitor_app:log_summary'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('<html', html.lower())
        # Check for a table or summary block
        self.assertRegex(html, r'<table|<div')