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
"""

import logging
import os
import threading

from django.utils import timezone

logger = logging.getLogger(__name__)

EPICPROD_APP_NAME = 'epicprod'

RESERVED_KEYS = ('action', 'subject_type', 'subject_key', 'username',
                 'outcome', 'duration_ms')


def log_epicprod_action(instance, action, *, subject_type='', subject_key='',
                        username='', outcome='ok', duration_ms=None,
                        message='', level=logging.INFO, **counts):
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
        message: optional human-readable one-liner; composed if omitted.
        level: python logging level; use logging.ERROR for failed actions.
        **counts: numeric counts worth recording (rows_added=..., etc.);
            reserved keys are ignored if passed here.

    Returns:
        The created AppLog row id, or None if the write failed.
    """
    from .models import AppLog

    extra = {
        'action': str(action),
        'outcome': str(outcome),
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
