from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status
from .models import MonitoredItem
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token

class TestMonitoredItemUpdateStatus(APITestCase):
    def setUp(self):
        self.agent = MonitoredItem.objects.create(
            name="test-agent",
            description="A test agent",
            status="UNKNOWN"
        )
        self.url = reverse('monitoreditem-update-status')

    def test_update_status_success(self):
        data = {
            "name": "test-agent",
            "status": "OK",
            "last_heartbeat": "2025-06-18T12:00:00Z"
        }
        response = self.client.post(self.url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, "OK")
        self.assertEqual(response.data["status"], "OK")
        self.assertEqual(response.data["last_heartbeat"], "2025-06-18T12:00:00Z")

    def test_update_status_missing_fields(self):
        data = {"name": "test-agent"}
        response = self.client.post(self.url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.data)

    def test_update_status_agent_not_found(self):
        data = {"name": "nonexistent-agent", "status": "OK"}
        response = self.client.post(self.url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('error', response.data)

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
