"""Prepare one existing buffered incident for explicit TeamComms publication."""

import json
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from teamcomms.comms.schemas import Audience

from monitor_app.models import AppLog, CapcomNotice


class Command(BaseCommand):
    help = "Prepare one observed SWF notice; does not publish or trigger work."

    def add_arguments(self, parser):
        parser.add_argument("--event-id", type=int, required=True, help="Existing AppLog ID")
        parser.add_argument("--subscriber", default="capcom")
        parser.add_argument("--source", default="swf-monitor:epic-devcloud.org/prod",
                            help="Stable installation namespace; retain it for retries")
        parser.add_argument("--audience", required=True, help="Explicit TC audience JSON object")
        parser.add_argument("--topic", default="", help="Optional TC message topic")
        parser.add_argument("--output", required=True, help="New private JSON payload file")

    def handle(self, *args, **options):
        event_id, subscriber = options["event_id"], options["subscriber"]
        if event_id <= 0 or not subscriber or len(subscriber) > 100:
            raise CommandError("Select a positive event ID and a valid subscriber")
        source = options["source"]
        if not source or len(source) > 160:
            raise CommandError("Source namespace must contain 1–160 characters")
        if len(options["topic"]) > 160:
            raise CommandError("Topic must contain at most 160 characters")
        try:
            audience = Audience.model_validate(json.loads(options["audience"]))
        except ValueError as error:
            raise CommandError("Audience must be a valid, explicit TC audience JSON object") from error
        dedup_key = f"event:{event_id}:{subscriber}"
        # Read at most two rows: ambiguous evidence must not select a random row.
        rows = list(CapcomNotice.objects.filter(
            subscriber=subscriber, dedup_key=dedup_key).order_by("id")[:2])
        if len(rows) != 1:
            raise CommandError("Expected exactly one buffered notice for this event/subscriber")
        notice = rows[0]
        event = AppLog.objects.filter(pk=event_id).only("timestamp").first()
        if event is None or timezone.is_naive(event.timestamp):
            raise CommandError("Source event and its timezone-aware timestamp are required")
        payload = {
            "source": source,
            "event_id": f"applog:{event_id}",
            "audience": audience.model_dump(mode="json", exclude_defaults=True),
            "observed_at": event.timestamp.isoformat(),
            "topic": options["topic"],
            "content": (
                f"Observed SWF completion/event: {notice.title}\n"
                "This reports an existing incident; it does not trigger a new job or action.\n"
                f"Severity: {notice.severity}\n"
                f"{notice.detail}\n"
                f"Source event: AppLog {event_id} ({dedup_key})\n"
                f"Observed at: {event.timestamp.isoformat()}\n"
                f"Buffered at: {notice.created_at.isoformat()}\n"
                f"{notice.url}"
            ),
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        output = Path(options["output"]).expanduser()
        try:
            # Never replace prepared evidence or print a credential-bearing config.
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
        except OSError as error:
            raise CommandError(f"Could not create prepared payload: {error}") from error
        self.stdout.write(f"Prepared AppLog {event_id} in {output}; no publication performed")
