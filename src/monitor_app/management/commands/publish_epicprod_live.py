"""The action-stream tailer service (systemd unit swf-epicprod-live).

Every cycle it runs one notice-routing pass (monitor_app/notice_router.py):
new action-stream events are matched against consumer-registered
subscriptions and delivered — buffered-pull rows for feed consumers, push
plugins for channels (docs/NOTICE_ROUTING.md). The #epicprod-live
Mattermost publication that used to live in this command is the
``mattermost-live`` push plugin (monitor_app/notice_plugins.py), selected
by the ``epicprod-live`` subscription; its channel remains the SysConfig
``epicprod_live_channel`` knob.

Operator knob, re-read every cycle so cadence changes need no deploy:
  epicprod_live_poll_seconds   (default 30)

Run: manage.py publish_epicprod_live   (systemd unit swf-epicprod-live)
"""
import logging
import time

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 30


class Command(BaseCommand):
    help = "Tail the action stream and route events to subscribers."

    def handle(self, *args, **options):
        logger.info("epicprod-live tailer started")
        while True:
            poll = DEFAULT_POLL_SECONDS
            try:
                from monitor_app.notice_router import route_new_events
                route_new_events()
                poll = self._poll_seconds()
            except Exception:
                logger.exception("notice routing cycle failed; continuing")
            time.sleep(max(poll, 5))

    def _poll_seconds(self):
        # get_setting seeds a missing key into the SysConfig document — the
        # knob in use is always visible on the System page, never hidden
        # behind a code default. An explicitly set but unusable value is a
        # failure and is logged, never silently replaced.
        from monitor_app.models import SysConfig
        poll = SysConfig.get_setting(
            'epicprod_live_poll_seconds', DEFAULT_POLL_SECONDS)
        try:
            return int(poll)
        except (TypeError, ValueError):
            logger.warning("epicprod_live_poll_seconds %r is not an "
                           "integer; using default %r", poll,
                           DEFAULT_POLL_SECONDS)
            return DEFAULT_POLL_SECONDS
