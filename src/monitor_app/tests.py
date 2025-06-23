from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from .models import SystemAgent
from .management.commands.get_token import Command as GetTokenCommand
from django.core.management import call_command
from io import StringIO

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
