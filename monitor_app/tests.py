from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status
from .models import MonitoredItem
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
from io import StringIO
from django.core.management import call_command
from django.test import TestCase

class MonitoredItemViewSetTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpassword')
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION='Token ' + self.token.key)

        self.item1 = MonitoredItem.objects.create(name="agent1", description="Test Agent 1", status="OK")
        self.item2 = MonitoredItem.objects.create(name="agent2", description="Test Agent 2", status="WARNING")
        self.list_url = reverse('monitoreditem-list')
        self.detail_url = reverse('monitoreditem-detail', kwargs={'pk': self.item1.pk})

    def test_list_monitored_items(self):
        """
        Ensure we can list all monitored items.
        """
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        self.assertEqual(response.data[0]['name'], self.item1.name)

    def test_retrieve_monitored_item(self):
        """
        Ensure we can retrieve a single monitored item.
        """
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['name'], self.item1.name)

    def test_create_monitored_item(self):
        """
        Ensure we can create a new monitored item.
        """
        data = {'name': 'agent3', 'description': 'A new agent', 'status': 'ERROR'}
        response = self.client.post(self.list_url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(MonitoredItem.objects.count(), 3)
        self.assertEqual(MonitoredItem.objects.get(name='agent3').status, 'ERROR')

    def test_update_monitored_item(self):
        """
        Ensure we can update an existing monitored item.
        """
        data = {'name': 'agent1_updated', 'description': 'Updated description', 'status': 'OK'}
        response = self.client.put(self.detail_url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.item1.refresh_from_db()
        self.assertEqual(self.item1.name, 'agent1_updated')
        self.assertEqual(self.item1.description, 'Updated description')

    def test_partial_update_monitored_item(self):
        """
        Ensure we can partially update an existing monitored item.
        """
        data = {'status': 'ERROR'}
        response = self.client.patch(self.detail_url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.item1.refresh_from_db()
        self.assertEqual(self.item1.status, 'ERROR')
        self.assertEqual(self.item1.name, 'agent1') # Name should be unchanged

    def test_delete_monitored_item(self):
        """
        Ensure we can delete a monitored item.
        """
        response = self.client.delete(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(MonitoredItem.objects.count(), 1)
        self.assertFalse(MonitoredItem.objects.filter(pk=self.item1.pk).exists())


class MonitorAppUITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='ui_user', password='password')
        self.item = MonitoredItem.objects.create(name="ui_agent", status="OK")

    def test_index_view_unauthenticated(self):
        """Ensure anonymous users can see the index page but not edit controls."""
        response = self.client.get(reverse('monitor_app:index'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertContains(response, "Login")
        self.assertNotContains(response, "Create New Item")

    def test_index_view_authenticated(self):
        """Ensure logged-in users see the index page with edit controls."""
        self.client.login(username='ui_user', password='password')
        response = self.client.get(reverse('monitor_app:index'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertContains(response, "Logout")
        self.assertContains(response, "Create New Item")

    def test_login_required_for_create_view(self):
        """Ensure create view requires login."""
        response = self.client.get(reverse('monitor_app:monitored_item_create'))
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertIn(reverse('login'), response.url)

    def test_create_monitored_item(self):
        """Ensure a logged-in user can create a new item via the form."""
        self.client.login(username='ui_user', password='password')
        initial_count = MonitoredItem.objects.count()
        data = {'name': 'new_ui_agent', 'status': 'WARNING', 'description': 'From UI test'}
        response = self.client.post(reverse('monitor_app:monitored_item_create'), data)
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(MonitoredItem.objects.count(), initial_count + 1)
        self.assertTrue(MonitoredItem.objects.filter(name='new_ui_agent').exists())

    def test_update_monitored_item(self):
        """Ensure a logged-in user can update an item via the form."""
        self.client.login(username='ui_user', password='password')
        data = {'name': 'updated_ui_agent', 'status': 'ERROR', 'description': self.item.description}
        response = self.client.post(reverse('monitor_app:monitored_item_update', kwargs={'pk': self.item.pk}), data)
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.item.refresh_from_db()
        self.assertEqual(self.item.name, 'updated_ui_agent')
        self.assertEqual(self.item.status, 'ERROR')

    def test_delete_monitored_item(self):
        """Ensure a logged-in user can delete an item."""
        self.client.login(username='ui_user', password='password')
        initial_count = MonitoredItem.objects.count()
        response = self.client.post(reverse('monitor_app:monitored_item_delete', kwargs={'pk': self.item.pk}))
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(MonitoredItem.objects.count(), initial_count - 1)
        self.assertFalse(MonitoredItem.objects.filter(pk=self.item.pk).exists())


class GetTokenCommandTest(TestCase):
    def test_get_token_for_existing_user(self):
        """Ensure the command retrieves a token for an existing user."""
        user = User.objects.create_user(username='token_user', password='password')
        out = StringIO()
        call_command('get_token', user.username, stdout=out)
        token = Token.objects.get(user=user)
        self.assertIn(token.key, out.getvalue())

    def test_get_token_and_create_user(self):
        """Ensure the command creates a new user and token with the --create-user flag."""
        out = StringIO()
        call_command('get_token', 'new_token_user', '--create-user', stdout=out)
        self.assertTrue(User.objects.filter(username='new_token_user').exists())
        user = User.objects.get(username='new_token_user')
        token = Token.objects.get(user=user)
        self.assertIn(token.key, out.getvalue())

    def test_get_token_user_not_found(self):
        """Ensure the command raises an error if the user does not exist and --create-user is not used."""
        with self.assertRaises(Exception):
            call_command('get_token', 'nonexistent_user')
