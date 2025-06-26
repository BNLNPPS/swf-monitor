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

class SystemAgentAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpassword')
        self.client.force_authenticate(user=self.user)
        self.agent = SystemAgent.objects.create(instance_name='test_agent', agent_type='test_type', status='OK')

    def test_list_agents(self):
        url = reverse('systemagent-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_create_agent(self):
        url = reverse('systemagent-list')
        data = {'instance_name': 'new_agent', 'agent_type': 'new_type', 'status': 'OK'}
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
        self.user = User.objects.create_user(username='testuser', password='testpassword')
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
        self.user = User.objects.create_user(username='ui_user', password='password')
        self.client.login(username='ui_user', password='password')
        now = timezone.now()
        AppLog.objects.create(app_name='app1', instance_name='inst1', level=logging.INFO, message='info message 1', timestamp=now, level_name='INFO', module='m', func_name='f', line_no=1, process=1, thread=1)
        AppLog.objects.create(app_name='app1', instance_name='inst1', level=logging.WARNING, message='warning message 1', timestamp=now, level_name='WARNING', module='m', func_name='f', line_no=1, process=1, thread=1)
        AppLog.objects.create(app_name='app1', instance_name='inst2', level=logging.ERROR, message='error message 1', timestamp=now, level_name='ERROR', module='m', func_name='f', line_no=1, process=1, thread=1)
        AppLog.objects.create(app_name='app2', instance_name='inst1', level=logging.INFO, message='info message 2', timestamp=now, level_name='INFO', module='m', func_name='f', line_no=1, process=1, thread=1)

    def test_log_summary_view(self):
        response = self.client.get(reverse('monitor_app:log_summary'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Log Summary')
        summary_data = response.context['summary']
        self.assertEqual(len(summary_data), 2) # There are 2 unique app_names: app1, app2

    def test_log_list_view(self):
        response = self.client.get(reverse('monitor_app:log_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Detailed Log View')
        self.assertEqual(len(response.context['page_obj']), 4)

    def test_log_list_view_filtered(self):
        response = self.client.get(reverse('monitor_app:log_list') + '?app_name=app1&instance_name=inst1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['page_obj']), 2)
        self.assertContains(response, 'info message 1')
        self.assertContains(response, 'warning message 1')
        self.assertNotContains(response, 'error message 1')

class MonitorAppUITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='ui_user', password='password')
        self.staff_user = User.objects.create_user(username='staff_user', password='password', is_staff=True)
        self.agent = SystemAgent.objects.create(instance_name='ui_agent', agent_type='ui_type', status='OK')

    def test_index_view_unauthenticated(self):
        response = self.client.get(reverse('monitor_app:index'))
        self.assertEqual(response.status_code, 302) # Redirect to login

    def test_index_view_authenticated(self):
        self.client.login(username='ui_user', password='password')
        response = self.client.get(reverse('monitor_app:index'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.agent.instance_name)

    def test_create_agent_view_as_staff(self):
        self.client.login(username='staff_user', password='password')
        url = reverse('monitor_app:system_agent_create')
        data = {'instance_name': 'new_ui_agent', 'agent_type': 'new_ui_type', 'status': 'OK'}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302) # Redirect on success
        self.assertTrue(SystemAgent.objects.filter(instance_name='new_ui_agent').exists())

    def test_create_agent_view_as_non_staff(self):
        self.client.login(username='ui_user', password='password')
        url = reverse('monitor_app:system_agent_create')
        data = {'instance_name': 'new_ui_agent', 'agent_type': 'new_ui_type', 'status': 'OK'}
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

class LogSummaryAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='loguser', password='logpass')
        self.client.force_authenticate(user=self.user)
        now = timezone.now()
        # Create logs for two apps and two instances
        AppLog.objects.create(app_name='app1', instance_name='inst1', timestamp=now, level=logging.ERROR, level_name='ERROR', message='Error 1', module='mod', func_name='f', line_no=1, process=1, thread=1)
        AppLog.objects.create(app_name='app1', instance_name='inst1', timestamp=now, level=logging.INFO, level_name='INFO', message='Info 1', module='mod', func_name='f', line_no=2, process=1, thread=1)
        AppLog.objects.create(app_name='app1', instance_name='inst2', timestamp=now, level=logging.ERROR, level_name='ERROR', message='Error 2', module='mod', func_name='f', line_no=3, process=1, thread=1)
        AppLog.objects.create(app_name='app2', instance_name='inst3', timestamp=now, level=logging.CRITICAL, level_name='CRITICAL', message='Critical 1', module='mod', func_name='f', line_no=4, process=1, thread=1)

    def test_log_summary(self):
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
