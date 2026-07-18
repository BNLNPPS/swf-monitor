from unittest.mock import patch

from django.test import TestCase

from snapper_ai.models import CurrentComponent

from monitor_app.snapper_panda import publish_panda_activity


ACTIVITY = {
    "jobs": {
        "total": 120,
        "by_status": {"finished": 70, "failed": 10, "running": 40},
        "by_user": [{"user": "not-published", "total": 120}],
        "by_site": [
            {
                "site": "SITE_A",
                "total": 100,
                "finished": 65,
                "failed": 5,
                "running": 30,
            },
            {
                "site": "SITE_B",
                "total": 20,
                "finished": 5,
                "failed": 5,
                "running": 10,
            },
        ],
    },
    "tasks": {
        "total": 8,
        "by_status": {"running": 5, "done": 3},
        "by_type": [
            {"type": "epicproduction", "count": 6},
            {"type": "stfprocessing", "count": 2},
        ],
        "by_user": [{"user": "not-published", "total": 8}],
    },
}

IN_FLIGHT = [
    {"site": "SITE_A", "status": "activated", "jobs": 50, "cores": 50},
    {"site": "SITE_A", "status": "running", "jobs": 30, "cores": 60},
    {"site": "SITE_B", "status": "starting", "jobs": 10, "cores": 10},
    {"site": "SITE_B", "status": "running", "jobs": 10, "cores": 10},
]

IN_FLIGHT_TASKS = [
    {"site": "SITE_A", "status": "running", "tasks": 5},
    {"site": "SITE_B", "status": "ready", "tasks": 2},
    {"site": "SITE_B", "status": "running", "tasks": 1},
]


class PandaPublicationTests(TestCase):
    @patch("monitor_app.snapper_panda._in_flight_task_activity")
    @patch("monitor_app.snapper_panda._in_flight_activity")
    @patch("monitor_app.snapper_panda.get_activity")
    def test_publishes_bounded_activity_without_user_or_record_detail(
        self,
        activity_query,
        in_flight_query,
        in_flight_task_query,
    ):
        activity_query.return_value = ACTIVITY
        in_flight_query.return_value = IN_FLIGHT
        in_flight_task_query.return_value = IN_FLIGHT_TASKS

        first = publish_panda_activity()
        component = CurrentComponent.objects.get(scope="epicprod", name="panda")
        self.assertTrue(first.update.content_changed)
        self.assertEqual(component.revision, 1)
        self.assertEqual(component.data["window_hours"], 24)
        self.assertEqual(component.data["jobs"]["total_24h"], 120)
        self.assertEqual(
            component.data["jobs"]["in_flight_now"],
            {
                "total": 100,
                "by_status": {
                    "activated": 50,
                    "running": 40,
                    "starting": 10,
                },
                "running_jobs": 40,
                "running_cores": 70,
            },
        )
        self.assertEqual(
            component.data["jobs"]["sites"]["SITE_A"]["failed_24h"],
            5,
        )
        self.assertEqual(
            component.data["jobs"]["sites"]["SITE_A"]["by_status_now"],
            {"activated": 50, "running": 30},
        )
        self.assertEqual(
            component.data["tasks"]["by_type_24h"],
            {"epicproduction": 6, "stfprocessing": 2},
        )
        self.assertEqual(
            component.data["tasks"]["in_flight_now"],
            {
                "total": 8,
                "by_status": {"ready": 2, "running": 6},
            },
        )
        self.assertEqual(
            component.data["tasks"]["sites"],
            {
                "SITE_A": {
                    "in_flight_tasks_now": 5,
                    "by_status_now": {"running": 5},
                },
                "SITE_B": {
                    "in_flight_tasks_now": 3,
                    "by_status_now": {"ready": 2, "running": 1},
                },
            },
        )
        self.assertNotIn("by_user", component.data["jobs"])
        self.assertNotIn("by_user", component.data["tasks"])

        unchanged = publish_panda_activity()
        component.refresh_from_db()
        self.assertFalse(unchanged.update.content_changed)
        self.assertEqual(component.revision, 1)

        in_flight_query.return_value = [
            {
                "site": "SITE_A",
                "status": "activated",
                "jobs": 50,
                "cores": 50,
            },
            {
                "site": "SITE_A",
                "status": "running",
                "jobs": 31,
                "cores": 62,
            },
            {
                "site": "SITE_B",
                "status": "starting",
                "jobs": 10,
                "cores": 10,
            },
            {
                "site": "SITE_B",
                "status": "running",
                "jobs": 10,
                "cores": 10,
            },
        ]
        changed = publish_panda_activity()
        component.refresh_from_db()
        self.assertTrue(changed.update.content_changed)
        self.assertEqual(component.revision, 2)
        self.assertEqual(
            component.data["jobs"]["in_flight_now"]["running_cores"],
            72,
        )
