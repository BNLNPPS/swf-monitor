"""Publish epicprod action-stream live events to a Mattermost channel.

The publisher is a polling tailer over the action stream: every cycle it
selects new records passing the live filter (`live_stream_q`), posts one
compact Mattermost message per event, and advances a high-water mark in
PersistentState. It posts under the DISpatcher bot token — the bot skips
its own posts, so events reach the channel without waking the bot; people
follow up by @mentioning DISpatcher in a thread under an event post, and
the post carries what the bot needs to drill in (action, subject, outcome,
reason, record link).

Operator knobs live in SysConfig and are re-read every cycle, so channel
rename, verbosity threshold, and cadence changes need no deploy:
  epicprod_live_channel        (default 'epicprod-live')
  epicprod_live_min_sublevel   (default 'normal')
  epicprod_live_poll_seconds   (default 30)

Run: manage.py publish_epicprod_live   (systemd unit swf-epicprod-live)
"""
import logging
import os
import time
from datetime import timezone as dt_timezone

import requests
from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)

MM_URL = os.environ.get('MATTERMOST_URL', 'chat.epic-eic.org')
MM_TOKEN = os.environ.get('MATTERMOST_TOKEN', '')
MM_TEAM = os.environ.get('MATTERMOST_TEAM', 'main')
# Open-face link base so event links work for the whole collaboration.
LINK_BASE = os.environ.get('EPICPROD_LIVE_LINK_BASE',
                           'https://epic-devcloud.org/prod')

STATE_KEY = 'epicprod_live_last_id'
DEFAULT_CHANNEL = 'epicprod-live'
DEFAULT_MIN_SUBLEVEL = 'normal'
DEFAULT_POLL_SECONDS = 30
BATCH_MAX = 20          # per-cycle post cap; overflow is summarized, not dropped
HTTP_TIMEOUT = 15


class Command(BaseCommand):
    help = "Tail the epicprod action stream and post live events to Mattermost."

    def handle(self, *args, **options):
        if not MM_TOKEN:
            self.stderr.write("MATTERMOST_TOKEN not set; cannot publish")
            raise SystemExit(2)
        self.session = requests.Session()
        self.session.headers['Authorization'] = f'Bearer {MM_TOKEN}'
        self.base = f'https://{MM_URL}/api/v4'
        self.team_id = self._get(f'/teams/name/{MM_TEAM}')['id']
        self.channel_name = ''
        self.channel_id = ''
        self._init_high_water()
        logger.info("epicprod-live publisher started (team %s)", MM_TEAM)
        while True:
            poll = DEFAULT_POLL_SECONDS
            try:
                poll = self._cycle()
            except Exception:
                logger.exception("publish cycle failed; continuing")
            time.sleep(max(int(poll), 5))

    # -- one cycle -----------------------------------------------------------

    def _cycle(self):
        from monitor_app.epicprod_logging import (
            SUBLEVEL_VALUES, live_stream_q)
        from monitor_app.models import AppLog, SysConfig

        config = SysConfig.get_config() or {}
        channel = str(config.get('epicprod_live_channel')
                      or DEFAULT_CHANNEL)
        min_sublevel = str(config.get('epicprod_live_min_sublevel')
                           or DEFAULT_MIN_SUBLEVEL)
        if min_sublevel not in SUBLEVEL_VALUES:
            min_sublevel = DEFAULT_MIN_SUBLEVEL
        poll = config.get('epicprod_live_poll_seconds') or DEFAULT_POLL_SECONDS

        if channel != self.channel_name:      # first cycle or painless rename
            self.channel_id = self._get(
                f'/teams/{self.team_id}/channels/name/{channel}')['id']
            self.channel_name = channel
            logger.info("publishing to #%s (%s)", channel, self.channel_id)

        last_id = self._get_high_water()
        rows = list(
            AppLog.objects.filter(live_stream_q(min_sublevel), id__gt=last_id)
            .order_by('id')[:BATCH_MAX + 1]
        )
        overflow = len(rows) > BATCH_MAX
        for row in rows[:BATCH_MAX]:
            self._post(self._format(row))
            self._set_high_water(row.id)       # per-record: no replay, no skip
        if overflow:
            newest = (AppLog.objects.filter(live_stream_q(min_sublevel))
                      .order_by('-id').values_list('id', flat=True).first())
            skipped = AppLog.objects.filter(
                live_stream_q(min_sublevel), id__gt=self._get_high_water(),
                id__lte=newest).count()
            self._post(f"… and {skipped} more events this cycle — see the "
                       f"[live view]({LINK_BASE}/logs/?app_name=epicprod&live=1)")
            self._set_high_water(newest)
        return poll

    # -- formatting ----------------------------------------------------------

    def _format(self, row):
        extra = row.extra_data if isinstance(row.extra_data, dict) else {}
        action = extra.get('action') or row.funcname or 'action'
        outcome = str(extra.get('outcome') or '')
        subject = ':'.join(x for x in (extra.get('subject_type'),
                                       extra.get('subject_key')) if x)
        username = str(extra.get('username') or '')
        reason = str(extra.get('reason') or '')
        dur = extra.get('duration_ms')
        stamp = timezone.localtime(row.timestamp).strftime('%H:%M')

        parts = [f"`{stamp}`", f"**{action}**"]
        if subject:
            parts.append(subject)
        parts.append(f"{row.instance_name}")
        if username:
            parts.append(f"by {username}")
        if outcome and outcome != 'ok':
            parts.append(f"⚠️ **{outcome.upper()}**")
        if reason:
            parts.append(reason)
        if isinstance(dur, (int, float)):
            parts.append(f"{dur / 1000:.1f} s")
        parts.append(f"[record]({LINK_BASE}/logs/{row.id}/)")
        return ' · '.join(parts)

    # -- plumbing ------------------------------------------------------------

    def _get(self, path):
        r = self.session.get(self.base + path, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _post(self, message):
        r = self.session.post(self.base + '/posts', timeout=HTTP_TIMEOUT,
                              json={'channel_id': self.channel_id,
                                    'message': message})
        r.raise_for_status()

    def _init_high_water(self):
        """Start at the current stream head — never replay history."""
        from monitor_app.models import AppLog, PersistentState
        state = PersistentState.get_state()
        if STATE_KEY not in state:
            head = (AppLog.objects.filter(app_name='epicprod')
                    .order_by('-id').values_list('id', flat=True).first()) or 0
            PersistentState.update_state({STATE_KEY: head})
            logger.info("initialized high-water mark at %s", head)

    def _get_high_water(self):
        from monitor_app.models import PersistentState
        return int(PersistentState.get_state().get(STATE_KEY) or 0)

    def _set_high_water(self, value):
        from monitor_app.models import PersistentState
        PersistentState.update_state({STATE_KEY: int(value)})
