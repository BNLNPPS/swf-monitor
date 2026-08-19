"""Push-plugin delivery for the notice router (docs/NOTICE_ROUTING.md).

A push plugin is a named in-process delivery mode: a subscription whose
``delivery`` value is a plugin name has each matched event handed to that
plugin during the routing pass. The router drives the pass protocol:
``start_pass()`` before a plugin's first delivery of the pass,
``deliver(row, extra)`` per matched event, ``end_pass()`` after the scan.

Push delivery is at-most-once: a delivery failure is logged and the pass
continues — the routing mark advances regardless, so a push outage never
stalls buffered-pull delivery. The event remains on the log record page.

The first plugin is ``mattermost-live``, the #epicprod-live channel
publisher moved here from the publish_epicprod_live command with its
formatting unchanged. Its channel is the SysConfig
``epicprod_live_channel`` knob, re-read every pass; its event selection
is the ``epicprod-live`` subscription, not code.
"""
import logging
import os
import re

import requests
from django.utils import timezone

logger = logging.getLogger(__name__)

MM_URL = os.environ.get('MATTERMOST_URL', 'chat.epic-eic.org')
# Post as the dedicated 'epicprod' identity; fall back to the DISpatcher bot
# token (posts then carry the DISpatcher face) until EPICPROD_LIVE_TOKEN is set.
MM_TOKEN = (os.environ.get('EPICPROD_LIVE_TOKEN')
            or os.environ.get('MATTERMOST_TOKEN', ''))
MM_TEAM = os.environ.get('MATTERMOST_TEAM', 'main')

DEFAULT_CHANNEL = 'epicprod-live'
PASS_POST_MAX = 20      # per-pass post cap; overflow is summarized, not dropped
HTTP_TIMEOUT = 15


def _link_base():
    """Open-face link base so event links work for the whole
    collaboration: env override, else the external-face configuration
    point plus the production path."""
    env = os.environ.get('EPICPROD_LIVE_LINK_BASE')
    if env:
        return env.rstrip('/')
    from monitor_app.models import external_face_base_url
    return f"{external_face_base_url()}/prod"


class MattermostLivePlugin:
    """The #epicprod-live channel as a router delivery mode."""

    UUID_RE = re.compile(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
        re.IGNORECASE)

    def __init__(self):
        self.session = None
        self.user_id = ''
        self.team_id = ''
        self.channel_name = ''
        self.channel_id = ''
        self._posted = 0
        self._skipped = 0

    # -- pass protocol -------------------------------------------------------

    def start_pass(self):
        """Connect on first use, honor a channel rename, reset the cap."""
        self._posted = 0
        self._skipped = 0
        if self.session is None:
            if not MM_TOKEN:
                raise RuntimeError(
                    "EPICPROD_LIVE_TOKEN/MATTERMOST_TOKEN not set")
            self.session = requests.Session()
            self.session.headers['Authorization'] = f'Bearer {MM_TOKEN}'
            self.base = f'https://{MM_URL}/api/v4'
            me = self._get('/users/me')
            self.user_id = me['id']
            self.team_id = self._get(f'/teams/name/{MM_TEAM}')['id']
            logger.info("mattermost-live plugin posting as @%s",
                        me.get('username'))
        from monitor_app.models import SysConfig
        channel = str(SysConfig.get_setting(
            'epicprod_live_channel', DEFAULT_CHANNEL) or '')
        if not channel:
            logger.warning("epicprod_live_channel is blank in SysConfig; "
                           "using default %r", DEFAULT_CHANNEL)
            channel = DEFAULT_CHANNEL
        if channel != self.channel_name:    # first pass or painless rename
            self.channel_id = self._get(
                f'/teams/{self.team_id}/channels/name/{channel}')['id']
            self.channel_name = channel
            try:                # self-join (idempotent, public channel)
                self.session.post(
                    f'{self.base}/channels/{self.channel_id}/members',
                    json={'user_id': self.user_id},
                    timeout=HTTP_TIMEOUT).raise_for_status()
            except Exception:
                logger.warning("could not self-join #%s", channel)
            logger.info("publishing to #%s (%s)", channel, self.channel_id)

    def deliver(self, row, extra):
        if self._posted >= PASS_POST_MAX:
            self._skipped += 1
            return
        self._post(self._format(row, extra))
        self._posted += 1

    def end_pass(self):
        if self._skipped:
            self._post(
                f"… and {self._skipped} more events this pass — see the "
                f"[live view]({_link_base()}/logs/?app_name=epicprod&live=1)")

    # -- formatting ----------------------------------------------------------

    def _format(self, row, extra):
        """One readable line per event: what happened, to what, by whom,
        and the one explanation that matters. Machine tokens stay on the
        record page — UUID subjects, the emitting component, and
        sub-10-second timings carry nothing for a channel reader."""
        action = extra.get('action') or row.funcname or 'action'
        outcome = str(extra.get('outcome') or '')
        if action == 'assessment_register' and outcome == 'ok':
            notice = self._format_assessment(row, extra)
            if notice:
                return notice
        subject_type = str(extra.get('subject_type') or '').replace('_', ' ')
        subject_key = str(extra.get('subject_key') or '')
        subject_label = str(extra.get('subject_label') or '')
        if subject_label:
            subject = subject_label
        else:
            if self.UUID_RE.search(subject_key):
                subject_key = ''
            subject = f'{subject_type} {subject_key}'.strip()
        username = str(extra.get('username') or '')
        reason = str(extra.get('reason') or '')
        summary = str(extra.get('summary') or '')
        dur = extra.get('duration_ms')
        stamp = timezone.localtime(row.timestamp).strftime('%H:%M')

        # The line's title is the most specific thing the event states
        # it did: its operation when it carries one, else the action.
        title = str(extra.get('operation') or action).replace('_', ' ')
        parts = [f"`{stamp}`", f"**{title}**"]
        if subject:
            parts.append(subject)
        if username:
            parts.append(f"by {username}")
        if outcome and outcome != 'ok':
            parts.append(f"⚠️ **{outcome.upper()}**")
        explanation = reason if (outcome and outcome != 'ok' and reason) \
            else summary
        if explanation:
            parts.append(explanation)
        if isinstance(dur, (int, float)) and dur >= 10000:
            parts.append(f"{dur / 1000:.1f} s")
        parts.append(f"[record]({_link_base()}/logs/{row.id}/)")
        return ' · '.join(parts)

    def _format_assessment(self, row, extra):
        """Linked publication notice; never duplicate the report body."""
        title = ' '.join(str(extra.get('report_title') or '').split())
        path = str(extra.get('report_path') or '').strip()
        if not title or not path.startswith('/ai/assessments/'):
            return ''
        url = f"{_link_base()}/{path.lstrip('/')}"
        stamp = timezone.localtime(row.timestamp).strftime('%H:%M %Z')
        subject_type = str(extra.get('subject_type') or '').strip()
        subject_key = str(extra.get('subject_key') or '').strip()
        subject = ''
        if subject_type and subject_key:
            subject = f'{subject_type.replace("_", " ").title()} {subject_key}'
        elif subject_key:
            subject = subject_key
        kind = str(extra.get('assessment_kind') or '').strip().lower()
        if kind == 'nightly':
            kind = 'daily'
        kind_label = kind.replace('_', ' ').title()
        verdict = str(extra.get('verdict') or '').strip()
        publication = (f'{kind_label} AI assessment published'
                       if kind_label else 'AI assessment published')
        parts = [f'`{stamp}`', f'**{publication}**']
        if subject:
            parts.append(subject)
        if verdict:
            verdict_text = f'Verdict: **{verdict.capitalize()}**'
            standing = extra.get('verdict_standing')
            if isinstance(standing, dict):
                prior = int(standing.get('prior_consecutive') or 0)
                if prior >= 1:
                    verdict_text += f' (standing, {prior + 1} consecutive)'
            parts.append(verdict_text)
        parts.append(f'[record]({_link_base()}/logs/{row.id}/)')
        notice = f'### [{title}]({url})\n' + ' · '.join(parts)
        narration = ' '.join(str(extra.get('narration') or '').split())
        if narration:
            notice += f'\n{narration}'
        return notice

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


PLUGINS = {'mattermost-live': MattermostLivePlugin()}
