"""epicprod action logging — the filterable action stream in AppLog.

Convention: ``app_name='epicprod'`` is the epicprod ACTION stream. Every
state-changing or operationally significant action — a catalog button press,
an MCP action tool, an ops-agent handler, a sweep, a submission, a report
generation — records one row here, regardless of which process performed it.
Process and infrastructure logs stay under their own app names; this stream
answers "what happened, and how did it go", and is the primary corpus
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

``sublevel`` — the event's declared importance (high | normal | low),
set at the call site, AUTHORITATIVE: changing it means changing the event.
It says which humans an event reaches — high reaches everyone including
email/digest audiences, normal reaches live-page watchers, low reaches only
the deliberately verbose viewer.

``live_default`` — the event's declared RECOMMENDATION for the live stream,
a special category: "interesting to some humans, now". The effective live
decision is the ``epicprod_live_policy`` override registry in SysConfig
(runtime attention knob, flipped on the live-policy page) over the default.

A channel = its importance threshold applied to live events:
``live_stream_q(min_sublevel=...)`` is the one filter every channel uses —
the deep live view takes all live events; an email digest takes live events
at importance high.
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
# live-stream recommendation, overridable at runtime. description: the
# plain-English one-liner answering "what is this action" — rendered on the
# log entry page and the live-policy page, so a stream reader is never left
# guessing what an event was.
ACTION_DEFAULTS = {
    # ops agent
    'task_submit': {
        'sublevel': 'high', 'live': True,
        'description': "Submit a campaign task to PanDA through the prun doer "
                       "under the production credential and record the "
                       "returned JEDI task id.",
    },
    'evgen_task_submit': {
        'sublevel': 'high', 'live': True,
        'description': "Submit an external-EVGEN campaign task to PanDA/JEDI "
                       "via the client API: build the task parameters, "
                       "assemble and upload the sandbox, record the JEDI "
                       "task id.",
    },
    'panda_task_operation': {
        'sublevel': 'high', 'live': True,
        'description': "Run a native PanDA operation on an existing JEDI "
                       "task: raise the allowed attempt count or retry "
                       "failed work.",
    },
    'rucio_sweep': {
        'sublevel': 'high', 'live': True,
        'description': "Refresh the JLab Rucio output snapshot for the "
                       "current (and last) campaign and rematch produced "
                       "RECO/FULL datasets onto each task's recorded outputs.",
    },
    'past_import': {
        'sublevel': 'high', 'live': True,
        'description': "Pull the eic/epic-prod bookkeeping clone and re-run "
                       "the idempotent FULL/RECO past-campaign output "
                       "ingest, keeping every campaign's recorded "
                       "production content current with what the "
                       "production team publishes.",
    },
    'rucio_arrivals': {
        'sublevel': 'normal', 'live': True,
        'description': "New files landed in JLab Rucio since the last "
                       "arrivals sweep, counted by campaign and location "
                       "across all versions — the signal behind the "
                       "derived 'producing' campaign status. Emitted only "
                       "when something arrived.",
    },
    'rucio_arrivals_sweep': {
        'sublevel': 'low', 'live': False,
        'description': "The clockwork arrivals-sweep step itself (outcome "
                       "and duration); the arrivals, when any, are the "
                       "rucio_arrivals event.",
    },
    'evgen_sweep': {
        'sublevel': 'high', 'live': True,
        'description': "Assimilate the JLab Rucio EVGEN input inventory "
                       "(epic:/EVGEN/*) and resolve each catalog request to "
                       "the registered input dataset(s) that realize it.",
    },
    'catalog_import': {
        'sublevel': 'high', 'live': True,
        'description': "Import the production CSV manifest catalog: create "
                       "or update campaign task records from the source rows.",
    },
    'association_sweep': {
        'sublevel': 'high', 'live': True,
        'description': "Associate recent PanDA tasks with campaign tasks; "
                       "unmatched direct group.EIC submissions are "
                       "auto-intaken as adopted tasks.",
    },
    'catalog_sync': {
        'sublevel': 'high', 'live': True,
        'description': "Nightly composite chain (cron 02:15): csv import, "
                       "epic-prod past ingest, questionnaire import, "
                       "association sweep, Rucio output snapshot, Rucio "
                       "arrivals sweep, EVGEN assimilation, questionnaire "
                       "automatch, match cache, progress refresh. This "
                       "record is the catalog-freshness timestamp.",
    },
    'agent_shutdown': {
        'sublevel': 'high', 'live': True,
        'description': "Deliberate stop of the production ops agent via the "
                       "message-bus back door; systemd leaves it stopped.",
    },
    'payload_log_fetch': {
        'sublevel': 'normal', 'live': False,
        'description': "Fetch one PanDA job's payload log tarball from Rucio "
                       "over xrootd and extract it into the shared cache for "
                       "the job page.",
    },
    'inventory_sync': {
        'sublevel': 'low', 'live': False,
        'description': "Refresh the monitor's ePIC production job/file "
                       "inventory and parsed failure diagnosis for a PanDA "
                       "job.",
    },
    'system_status_refresh': {
        'sublevel': 'low', 'live': False,
        'description': "Periodic refresh of the cached System page status "
                       "rows for services, agents, and external monitor "
                       "endpoints.",
    },
    'questionnaire_import': {
        'sublevel': 'normal', 'live': True,
        'description': "Import the PWG/DSC production-request questionnaire "
                       "CSV: create or update request records.",
    },
    'questionnaire_automatch': {
        'sublevel': 'normal', 'live': True,
        'description': "LLM matching of production requests to catalog tasks "
                       "using the full tag map; each new match logs its own "
                       "questionnaire_match_found event.",
    },
    'questionnaire_match_found': {
        'sublevel': 'normal', 'live': True,
        'description': "One new request-to-task match from the automatch, "
                       "with confidence and reason; high/medium confidence "
                       "lands accepted, low lands suggested.",
    },
    'questionnaire_match': {
        'sublevel': 'low', 'live': False,
        'description': "Rebuild the questionnaire-to-task match cache read "
                       "by request and task pages.",
    },
    'progress_refresh': {
        'sublevel': 'low', 'live': False,
        'description': "Rebuild current-campaign progress data and its "
                       "rendered progress table cache.",
    },
    # web and MCP
    'sysconfig_edit': {
        'sublevel': 'high', 'live': True,
        'description': "Operator edit of the SysConfig system-configuration "
                       "document on the System page.",
    },
    'live_policy_edit': {
        'sublevel': 'high', 'live': True,
        'description': "Operator change to an action's live-stream override "
                       "on the live-policy page.",
    },
    'assessment_register': {
        'sublevel': 'normal', 'live': True,
        'description': "Register an AI assessment of a production object "
                       "(campaign task, PanDA task, job, or queue).",
    },
    'assessment_link': {
        'sublevel': 'low', 'live': False,
        'description': "Link an existing AI assessment to a production "
                       "object's record.",
    },
    'task_set_status': {
        'sublevel': 'normal', 'live': True,
        'description': "Campaign task lifecycle transition (draft, ready, "
                       "submitted, completed, failed) with rule enforcement.",
    },
    'task_intake': {
        'sublevel': 'normal', 'live': True,
        'description': "Idempotent intake of a production request into a "
                       "draft campaign task.",
    },
    'dataset_intake': {
        'sublevel': 'normal', 'live': True,
        'description': "Idempotent find-or-create of a dataset from a source "
                       "location such as a CSV manifest.",
    },
    'task_link_input': {
        'sublevel': 'low', 'live': False,
        'description': "Link an existing dataset as a campaign task's input "
                       "by DID.",
    },
    'dataset_propagation_set': {
        'sublevel': 'normal', 'live': True,
        'description': "Operator change of dataset propagation disposition "
                       "(continue, hold, final) with required comment; one "
                       "event per single or bulk action, carrying the "
                       "changed count and the selecting filter. Carries an "
                       "origin stamp when executing an approved AI proposal.",
    },
    'proposal_created': {
        'sublevel': 'normal', 'live': True,
        'description': "AI proposal of a dataset propagation change, pending "
                       "human review; one event per propose call with the "
                       "proposed count and batch.",
    },
    'proposal_denied': {
        'sublevel': 'normal', 'live': True,
        'description': "Human denial of pending AI proposals; denial memory "
                       "prevents re-proposal until the proposer's inputs "
                       "change.",
    },
    'proposal_expired': {
        'sublevel': 'normal', 'live': True,
        'description': "Withdrawal of pending AI proposals (recurring-scan "
                       "heartbeat refresh or operator clear), with count.",
    },
    'proposal_deleted': {
        'sublevel': 'normal', 'live': False,
        'description': "Operator deletion of AI proposal list rows — "
                       "housekeeping for test or noise rows; removes audit "
                       "rows, logged with count.",
    },
    'narrative_edited': {
        'sublevel': 'normal', 'live': False,
        'description': "Expert revision of a campaign narrative document — "
                       "a new corun-ai version of the entry.",
    },
    'narrative_commented': {
        'sublevel': 'normal', 'live': True,
        'description': "Comment posted on a campaign narrative — the "
                       "non-intrusive contribution path beside editing.",
    },
    # web entity lifecycle and operator actions
    'campaign_set_current': {
        'sublevel': 'high', 'live': True,
        'description': "Operator designation of the current campaign.",
    },
    'campaign_set_last': {
        'sublevel': 'high', 'live': True,
        'description': "Operator designation of the last (previous) campaign.",
    },
    'questionnaire_match_add': {
        'sublevel': 'normal', 'live': True,
        'description': "Operator addition of a request-to-task match on the "
                       "request page.",
    },
    'questionnaire_match_remove': {
        'sublevel': 'normal', 'live': True,
        'description': "Operator removal of a request-to-task match.",
    },
    'category_create': {
        'sublevel': 'normal', 'live': True,
        'description': "Create a physics tag category.",
    },
    'tag_create': {
        'sublevel': 'low', 'live': False,
        'description': "Create a configuration tag (physics, evgen, simu, "
                       "reco, or background).",
    },
    'tag_edit': {
        'sublevel': 'low', 'live': False,
        'description': "Edit a draft configuration tag.",
    },
    'tag_lock': {
        'sublevel': 'normal', 'live': True,
        'description': "Lock a configuration tag — a permanent, one-way "
                       "transition to immutability.",
    },
    'tag_delete': {
        'sublevel': 'normal', 'live': True,
        'description': "Delete a draft configuration tag.",
    },
    'dataset_create': {
        'sublevel': 'normal', 'live': True,
        'description': "Create a dataset: a composed tag identity plus any "
                       "sample-variant discriminator.",
    },
    'dataset_block_add': {
        'sublevel': 'low', 'live': False,
        'description': "Add the next Rucio block (.bN) to a dataset.",
    },
    'config_create': {
        'sublevel': 'low', 'live': False,
        'description': "Create a production config template.",
    },
    'config_edit': {
        'sublevel': 'low', 'live': False,
        'description': "Edit a production config template.",
    },
    'task_delete': {
        'sublevel': 'normal', 'live': True,
        'description': "Delete a campaign task.",
    },
    'assessment_quality_set': {
        'sublevel': 'normal', 'live': True,
        'description': "Operator quality review (good, poor, wrong) recorded "
                       "on an AI assessment.",
    },
    'queues_update': {
        'sublevel': 'normal', 'live': True,
        'description': "Update PanDA queue records from the GitHub source.",
    },
    'endpoints_update': {
        'sublevel': 'normal', 'live': True,
        'description': "Update Rucio storage endpoint records from the "
                       "GitHub source.",
    },
}


def action_description(action):
    """Plain-English one-liner for a known action id, or ''."""
    return (ACTION_DEFAULTS.get(str(action)) or {}).get('description', '')

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
        sublevel: the event's declared importance — 'high' (reaches
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
        policy = SysConfig.get_setting(LIVE_POLICY_STATE_KEY, {}) or {}
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
            'description': decl.get('description', ''),
            'sublevel': decl['sublevel'],
            'live_default': decl['live'],
            'state': 'default' if override is None else ('live' if override else 'quiet'),
            'effective': bool(effective),
        })
    return rows


def live_stream_q(min_sublevel=None):
    """Q filter selecting live action records, optionally importance-gated.

    Live = the runtime override (epicprod_live_policy) over the record's
    declared live_default. A channel applies its importance threshold through
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
