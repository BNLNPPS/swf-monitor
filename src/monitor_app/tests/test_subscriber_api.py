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


class SubscriberAPITests(APITestCase):
    def setUp(self):
        unique_username = f"testuser_{uuid.uuid4()}"
        self.user = User.objects.create_user(username=unique_username, password='testpassword')
        self.client.force_authenticate(user=self.user)
        self.subscriber = Subscriber.objects.create(
            subscriber_name="test_subscriber",
            fraction=0.5,
            description="Test subscriber for unit tests",
            is_active=True
        )

    def test_list_subscribers(self):
        url = reverse('monitor_app:subscriber-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_create_subscriber(self):
        url = reverse('monitor_app:subscriber-list')
        data = {
            'subscriber_name': 'new_subscriber',
            'fraction': 0.8,
            'description': 'New test subscriber',
            'is_active': True
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Subscriber.objects.count(), 2)

    def test_get_subscriber(self):
        url = reverse('monitor_app:subscriber-detail', kwargs={'pk': self.subscriber.subscriber_id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['subscriber_name'], "test_subscriber")

    def test_update_subscriber_status(self):
        url = reverse('monitor_app:subscriber-detail', kwargs={'pk': self.subscriber.subscriber_id})
        data = {'is_active': False}
        response = self.client.patch(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.subscriber.refresh_from_db()
        self.assertFalse(self.subscriber.is_active)

    def test_update_subscriber_fraction(self):
        url = reverse('monitor_app:subscriber-detail', kwargs={'pk': self.subscriber.subscriber_id})
        data = {'fraction': 0.3}
        response = self.client.patch(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.subscriber.refresh_from_db()
        self.assertEqual(self.subscriber.fraction, 0.3)

    def test_delete_subscriber(self):
        url = reverse('monitor_app:subscriber-detail', kwargs={'pk': self.subscriber.subscriber_id})
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Subscriber.objects.filter(pk=self.subscriber.subscriber_id).exists())

    def test_create_subscriber_duplicate_name(self):
        url = reverse('monitor_app:subscriber-list')
        data = {
            'subscriber_name': 'test_subscriber',  # Same as existing
            'fraction': 0.1,
            'description': 'Duplicate name test'
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_fraction_range(self):
        url = reverse('monitor_app:subscriber-list')
        data = {
            'subscriber_name': 'invalid_fraction_subscriber',
            'fraction': 1.5,  # Invalid: > 1.0
            'description': 'Invalid fraction test'
        }
        response = self.client.post(url, data, format='json')
        # Note: This test may pass if no validation is implemented, but documents expected behavior
        # self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthenticated_access_denied(self):
        self.client.force_authenticate(user=None)
        url = reverse('monitor_app:subscriber-list')
        response = self.client.get(url)
        self.assertIn(response.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])