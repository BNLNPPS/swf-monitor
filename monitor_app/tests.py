from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status
from .models import MonitoredItem

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
