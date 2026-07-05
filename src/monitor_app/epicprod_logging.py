"""epicprod action logging — the filterable action stream in AppLog.

Convention: ``app_name='epicprod'`` is the epicprod ACTION stream. Every
state-changing or operationally significant action — a catalog button press,
an MCP action tool, an ops-agent handler, a sweep, a submission, a report
generation — records one row here, regardless of which process performed it.
Process and infrastructure logs stay under their own app names; this stream
answers "who did what to what, and how did it go", and is the primary corpus
for LLM assessment and reporting.

``instance_name`` names the component performing the action: 'web',
'ops-agent', 'mcp', 'catalog-sync', 'submit', 'report'.

Structured fields live in ``extra_data``; reserved keys are ``action``,
``subject_type``, ``subject_key``, ``username``, ``outcome``, and
``duration_ms``. Additional numeric counts (rows_added=..., tasks_matched=...)
are stored alongside them. Timed actions (sweeps in particular) must pass
``duration_ms``.

This helper writes via the ORM and serves in-process Django contexts (web
tier, MCP ASGI worker, management commands). Out-of-process callers such as
the ops agent record actions through the existing REST log endpoint with the
same app_name and field conventions.

Retrieval: the ``epicprod_list_actions`` MCP tool (filtered and summarized),
``swf_list_logs(app_name='epicprod')`` (raw), and the Logs UI filtered on
app_name.

Publication axes (log level is separate and keeps its universal meaning):

``sublevel`` — the event's declared verbosity class (high | normal | low),
set at the call site, AUTHORITATIVE: changing it means changing the event.
It says which humans an event reaches — high reaches everyone including
email/digest audiences, normal reaches live-page watchers, low reaches only
the deliberately verbose viewer.

``live_default`` — the event's declared RECOMMENDATION for the live stream,
a special category: "interesting to some humans, now". The effective live
decision is the ``epicprod_live_policy`` override registry in SysConfig
(runtime attention knob, flipped on the live-policy page) over the default.

A channel = its verbosity setting applied to live events:
``live_stream_q(min_sublevel=...)`` is the one filter every channel uses —
the deep live view takes all live events; an email digest takes live events
at sublevel high.
"""

import logging
import os
import threading

from django.utils import timezone

logger = logging.getLogger(__name__)

EPICPROD_APP_NAME = 'epicprod'

LIVE_POLICY_STATE_KEY = 'epicprod_live_policy'

SUBLEVEL_VALUES = ('high', 'normal', 'low')
SUBLEVEL_ORDER = {'high': 2, 'normal': 1, 'low': 0}

# Catalog of known actions and their call-site declarations. This MUST mirror
# the call sites (records carry the authoritative stamp; filters read the
# record) — it exists so the live-policy page can show every known action
# before/without records, and so new actions are declared in one greppable
# place. sublevel: high = reaches everyone (news, digests), normal =
# live-page watchers, low = verbose viewers only. live: the declared
# live-stream recommendation, overridable at runtime.
ACTION_DEFAULTS = {
    # ops agent
    'task_submit': {'sublevel': 'high', 'live': True},
    'evgen_task_submit': {'sublevel': 'high', 'live': True},
    'panda_task_operation': {'sublevel': 'high', 'live': True},
    'rucio_sweep': {'sublevel': 'high', 'live': True},
    'evgen_sweep': {'sublevel': 'high', 'live': True},
    'catalog_import': {'sublevel': 'high', 'live': True},
    'agent_shutdown': {'sublevel': 'high', 'live': True},
    'payload_log_fetch': {'sublevel': 'low', 'live': False},
    'inventory_sync': {'sublevel': 'low', 'live': False},
    'system_status_refresh': {'sublevel': 'low', 'live': False},
    'questionnaire_match': {'sublevel': 'low', 'live': False},
    'progress_refresh': {'sublevel': 'low', 'live': False},
    # web and MCP
    'sysconfig_edit': {'sublevel': 'high', 'live': True},
    'live_policy_edit': {'sublevel': 'high', 'live': True},
    'assessment_register': {'sublevel': 'normal', 'live': True},
    'assessment_link': {'sublevel': 'low', 'live': False},
    'task_set_status': {'sublevel': 'normal', 'live': True},
    'task_intake': {'sublevel': 'normal', 'live': True},
    'dataset_intake': {'sublevel': 'normal', 'live': True},
    'task_link_input': {'sublevel': 'low', 'live': False},
}

RESERVED_KEYS = ('action', 'subject_type', 'subject_key', 'username',
                 'outcome', 'duration_ms', 'sublevel', 'live_default')


def log_epicprod_action(instance, action, *, subject_type='', subject_key='',
                        username='', outcome='ok', duration_ms=None,
                        sublevel='low', live_default=False, message='',
                        level=logging.INFO, **counts):
    """Record one epicprod action in the AppLog action stream.

    Never raises: a failure to record is logged to the module logger and the
    calling action proceeds — the action log must not break the action.

    Args:
        instance: component performing the action ('web', 'ops-agent', 'mcp',
            'catalog-sync', 'submit', 'report').
        action: short action identifier, e.g. 'rucio_sweep', 'task_submit',
            'assessment_register'.
        subject_type: acted-on object type when there is one (canonical
            assessment subject types where applicable).
        subject_key: acted-on object key (composed name, JEDI id, queue, ...).
        username: human or service account driving the action.
        outcome: 'ok' or 'error' (conventional; free-form refinements allowed).
        duration_ms: measured execution time; required in spirit for sweeps
            and other timed operations.
        sublevel: the event's declared verbosity class — 'high' (reaches
            everyone: news, digests, email), 'normal' (live-page watchers),
            'low' (verbose viewers only). AUTHORITATIVE: change it by
            changing the event, not at runtime.
        live_default: the event's declared RECOMMENDATION for the live
            stream ("interesting to some humans, now"). The effective live
            decision is the epicprod_live_policy override (runtime attention
            knob) over this default.
        message: optional human-readable one-liner; composed if omitted.
        level: python logging level; use logging.ERROR for failed actions.
        **counts: numeric counts worth recording (rows_added=..., etc.);
            reserved keys are ignored if passed here.

    Returns:
        The created AppLog row id, or None if the write failed.
    """
    from .models import AppLog

    if sublevel not in SUBLEVEL_VALUES:
        logger.warning('epicprod action %s: unknown sublevel %r, using low',
                       action, sublevel)
        sublevel = 'low'
    extra = {
        'action': str(action),
        'outcome': str(outcome),
        'sublevel': sublevel,
        'live_default': bool(live_default),
    }
    if subject_type:
        extra['subject_type'] = str(subject_type)
    if subject_key:
        extra['subject_key'] = str(subject_key)
    if username:
        extra['username'] = str(username)
    if duration_ms is not None:
        try:
            extra['duration_ms'] = int(duration_ms)
        except (TypeError, ValueError):
            logger.warning('epicprod action %s: non-numeric duration_ms %r',
                           action, duration_ms)
    for key, value in counts.items():
        if key not in RESERVED_KEYS:
            extra[key] = value

    if not message:
        subject = f"{subject_type}:{subject_key}" if subject_key else ''
        message = ' '.join(part for part in (str(action), subject, str(outcome)) if part)

    try:
        row = AppLog.objects.create(
            app_name=EPICPROD_APP_NAME,
            instance_name=str(instance),
            timestamp=timezone.now(),
            level=int(level),
            levelname=logging.getLevelName(int(level)),
            message=message,
            module='epicprod_logging',
            funcname=str(action),
            lineno=0,
            process=os.getpid(),
            thread=threading.get_ident(),
            extra_data=extra,
        )
        return row.id
    except Exception:
        logger.exception('epicprod action log write failed: %s %s',
                         instance, action)
        return None


def get_live_policy():
    """Return the live override registry: {action_id: bool}.

    Stored under LIVE_POLICY_STATE_KEY in SysConfig (system configuration
    lives in the DB, adjustable via the live-policy page). The runtime
    attention knob: an action absent from the registry follows its records'
    live_default recommendation. Reads defensively: a missing table
    (pre-migration) yields an empty policy with a logged warning rather than
    breaking the caller.
    """
    from .models import SysConfig

    try:
        policy = SysConfig.get_config().get(LIVE_POLICY_STATE_KEY) or {}
    except Exception as exc:
        logger.warning('live policy read failed (empty policy used): %s', exc)
        return {}
    if not isinstance(policy, dict):
        return {}
    return {str(k): bool(v) for k, v in policy.items()}


def set_live_policy_entry(action, value, username=''):
    """Set (True/False) or clear (None) an action's live override."""
    from .models import SysConfig

    policy = get_live_policy()
    key = str(action)
    if value is None:
        policy.pop(key, None)
    else:
        policy[key] = bool(value)
    SysConfig.update_config({LIVE_POLICY_STATE_KEY: policy}, username=username)
    return policy


def live_policy_rows():
    """Rows for the live-policy page: every known action with its declared
    sublevel and live default, any live override, and the effective state.

    Known = the ACTION_DEFAULTS catalog plus any action observed in the
    stream (the newest record supplies declarations for actions not yet in
    the catalog).
    """
    from .models import AppLog

    policy = get_live_policy()
    declarations = {k: dict(v) for k, v in ACTION_DEFAULTS.items()}
    observed = (AppLog.objects.filter(app_name=EPICPROD_APP_NAME)
                .exclude(extra_data__action__isnull=True)
                .values_list('extra_data__action', flat=True)
                .distinct())
    for action in observed:
        if action and action not in declarations:
            latest = (AppLog.objects.filter(app_name=EPICPROD_APP_NAME,
                                            extra_data__action=action)
                      .order_by('-id').first())
            extra = latest.extra_data if latest and isinstance(latest.extra_data, dict) else {}
            stamped = extra.get('sublevel')
            declarations[action] = {
                'sublevel': stamped if stamped in SUBLEVEL_VALUES else 'low',
                'live': bool(extra.get('live_default')),
            }

    rows = []
    for action in sorted(declarations):
        decl = declarations[action]
        override = policy.get(action)
        effective = override if override is not None else decl['live']
        rows.append({
            'action': action,
            'sublevel': decl['sublevel'],
            'live_default': decl['live'],
            'state': 'default' if override is None else ('live' if override else 'quiet'),
            'effective': bool(effective),
        })
    return rows


def live_stream_q(min_sublevel=None):
    """Q filter selecting live action records, optionally verbosity-gated.

    Live = the runtime override (epicprod_live_policy) over the record's
    declared live_default. A channel applies its verbosity setting through
    min_sublevel: the deep live view passes None (all live events); an email
    digest passes 'high'. The single source of stream semantics for every
    channel.
    """
    from django.db.models import Q

    policy = get_live_policy()
    force_on = [a for a, v in policy.items() if v]
    force_off = [a for a, v in policy.items() if not v]

    q = Q(extra_data__live_default=True)
    if force_off:
        q &= ~Q(extra_data__action__in=force_off)
    if force_on:
        q |= Q(extra_data__action__in=force_on)
    if min_sublevel in SUBLEVEL_VALUES:
        min_rank = SUBLEVEL_ORDER[min_sublevel]
        ge_values = [v for v in SUBLEVEL_VALUES if SUBLEVEL_ORDER[v] >= min_rank]
        q &= Q(extra_data__sublevel__in=ge_values)
    return Q(app_name=EPICPROD_APP_NAME) & q
