from datetime import timedelta

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from snapper_ai.models import CurrentComponent
from monitor_app.workflow_models import WorkflowDefinition, WorkflowExecution


class DatatakingPublicationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="snapper-datataking-test",
            password="testpassword",
        )
        self.client.force_authenticate(user=self.user)
        self.initial_transition = timezone.now() - timedelta(minutes=1)
        self.workflow_definition = WorkflowDefinition.objects.create(
            workflow_name="state-history-test",
            version="1",
            workflow_type="test",
            definition="pass",
            parameter_values={},
            created_by=self.user.username,
        )
        self._create_execution("state-history-alice", "alice")
        self.run_data = {
            "run_number": 101,
            "phase": "initializing",
            "state": "imminent",
            "substate": "preparing",
            "target_worker_count": 2,
            "active_worker_count": 0,
            "stf_samples_received": 0,
            "slices_created": 0,
            "slices_queued": 0,
            "slices_processing": 0,
            "slices_completed": 0,
            "slices_failed": 0,
            "state_changed_at": self.initial_transition.isoformat(),
            "metadata": {"execution_id": "state-history-alice"},
        }

    def _create_execution(self, execution_id, namespace):
        return WorkflowExecution.objects.create(
            execution_id=execution_id,
            workflow_definition=self.workflow_definition,
            namespace=namespace,
            parameter_values={},
            status="running",
            start_time=self.initial_transition,
            executed_by=self.user.username,
        )

    def test_only_state_transitions_advance_datataking_revision(self):
        list_url = reverse("monitor_app:runstate-list")
        response = self.client.post(list_url, self.run_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        component = CurrentComponent.objects.get(
            scope="testbed",
            name="datataking",
        )
        self.assertEqual(component.component_schema_version, 2)
        self.assertEqual(component.revision, 1)
        self.assertEqual(
            component.data["namespaces"]["alice"],
            {
                "run_number": 101,
                "phase": "initializing",
                "state": "imminent",
                "substate": "preparing",
                "last_transition_at": (
                    self.initial_transition.isoformat().replace("+00:00", "Z")
                ),
            },
        )

        detail_url = reverse("monitor_app:runstate-detail", kwargs={"pk": 101})
        counter_time = self.initial_transition + timedelta(seconds=10)
        response = self.client.patch(
            detail_url,
            {
                "slices_created": 4,
                "state_changed_at": counter_time.isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        component.refresh_from_db()
        self.assertEqual(component.revision, 1)
        self.assertEqual(
            component.data["namespaces"]["alice"]["last_transition_at"],
            self.initial_transition.isoformat().replace("+00:00", "Z"),
        )

        transition_time = counter_time + timedelta(seconds=10)
        response = self.client.patch(
            detail_url,
            {
                "phase": "physics",
                "state": "running",
                "substate": "physics",
                "state_changed_at": transition_time.isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        component.refresh_from_db()
        self.assertEqual(component.revision, 2)
        alice = component.data["namespaces"]["alice"]
        self.assertEqual(alice["phase"], "physics")
        self.assertEqual(alice["state"], "running")
        self.assertEqual(alice["substate"], "physics")
        self.assertEqual(
            alice["last_transition_at"],
            transition_time.isoformat().replace("+00:00", "Z"),
        )

        self._create_execution("state-history-bob", "bob")
        bob_data = {
            **self.run_data,
            "run_number": 102,
            "metadata": {"execution_id": "state-history-bob"},
        }
        response = self.client.post(list_url, bob_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        component.refresh_from_db()
        self.assertEqual(component.revision, 3)
        self.assertEqual(set(component.data["namespaces"]), {"alice", "bob"})
        self.assertEqual(
            component.data["namespaces"]["alice"]["run_number"],
            101,
        )
        self.assertEqual(
            component.data["namespaces"]["bob"]["run_number"],
            102,
        )
