#!/usr/bin/env python3
"""live-watch-enforce.py — accept a live watch run's result.

The enforcement end of the live watch (swf-epicprod
docs/EPICPROD_ASSESSMENTS.md, The live watch), on the segfault diagnosis's
pattern: on the corun completion callback the ops agent runs this with the
job, the prompt group and the result page. The artifact is extracted and
validated against the schema and the floor; on failure one bounded repair
run receives the exact problems and the prior output, and a second failure
quarantines it.

A valid run is kept as the cached product ``live_watch`` (the latest
report) and recorded as a ``live_watch`` action. It is registered as an
assessment, and its action marked ``notify`` for the Capcom feed, only
when what it found changed from the last registered run: a new verdict,
a new real problem, new noise, or the return to clean after a registered
finding. An alarm verdict reaches the channel; nothing else the watch
does posts there.

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/live-watch-enforce.py \\
        --job-id ID --prompt-group-id GID --page-group-id PGID --status completed
"""
import argparse
import json
import logging
import os
import sys
import time

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from django.utils import timezone  # noqa: E402

from monitor_app.cached_product import get_product  # noqa: E402
from monitor_app.epicprod_logging import log_epicprod_action  # noqa: E402
from monitor_app.mcp.ai_content import _register_ai_assessment_sync  # noqa: E402
from monitor_app.models import PersistentState  # noqa: E402
from swf_epicprod.assessment.bundle import _get  # noqa: E402
from swf_epicprod.assessment.trigger import (CORUN_API_TOKEN, CORUN_API_URL,  # noqa: E402
                                             _request)
from swf_epicprod.livewatch import spec  # noqa: E402

SECTION = os.environ.get('CORUN_LIVEWATCH_SECTION', spec.DEFAULT_SECTION)
DEFINITION = os.environ.get('CORUN_LIVEWATCH_DEFINITION', '')
CHANNEL_URL = 'https://chat.epic-eic.org/main/channels/epicprod-live'
SEVERITY = {'ok': 'info', 'attention': 'warning', 'alarm': 'alarm'}
PRODUCT_KEY = 'live_watch'
PRODUCT_TTL_S = 7 * 24 * 3600
log = logging.getLogger('live-watch-enforce')


def _state():
    return dict(PersistentState.get_state().get('live_watch') or {})


def _save(state):
    PersistentState.update_state({'live_watch': state})


def _mark(state, run_state, **fields):
    current = dict(state.get('current') or {})
    current['state'] = run_state
    current.update(fields)
    state['current'] = current
    _save(state)


def _log(outcome, *, notify=False, live=False, username='', **counts):
    log_epicprod_action('live-watch', 'live_watch', outcome=outcome,
                        subject_type='live_channel', subject_key='epicprod-live',
                        username=username, sublevel='high' if live else ('normal' if notify else 'low'),
                        live_default=live, notify=notify,
                        level=logging.ERROR if outcome == 'error' else logging.INFO, **counts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--job-id', required=True)
    ap.add_argument('--prompt-group-id', required=True)
    ap.add_argument('--page-group-id', default='')
    ap.add_argument('--status', required=True)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    t0 = time.monotonic()
    state = _state()
    requested_by = (state.get('current') or {}).get('requested_by') or ''

    prompt = _get(f'{CORUN_API_URL}/prompts/{args.prompt_group_id}/', token=CORUN_API_TOKEN)
    try:
        submitted = json.loads(prompt.get('content') or '{}')
    except json.JSONDecodeError:
        submitted = {}
    bundle = submitted.get('bundle') or {}

    if args.status != 'completed':
        _mark(state, 'failed', job_id=args.job_id, reason=f'run ended {args.status}')
        _log('error', username=requested_by, reason=f'corun run {args.job_id} ended {args.status}')
        print(json.dumps({'outcome': 'failed', 'status': args.status}))
        return 1

    page = _get(f'{CORUN_API_URL}/pages/{args.page_group_id}/', token=CORUN_API_TOKEN)
    content = page.get('content') or ''
    artifact, remainder, problems = spec.extract_artifact(content)
    if artifact is not None:
        problems += spec.validate_artifact(artifact, bundle)
        problems += spec.validate_remainder(remainder)

    if problems:
        if not submitted.get('repair'):
            repair = dict(submitted)
            repair['repair'] = {'validation_problems': problems, 'previous_output': content,
                                'instruction': 'Produce a complete replacement JSON artifact that '
                                               'corrects every listed validation problem while '
                                               'preserving the supported findings.'}
            retry_prompt = _request(f'{CORUN_API_URL}/prompts/',
                                    payload={'section': SECTION, 'content': json.dumps(repair, default=str),
                                             'definition_id': DEFINITION},
                                    token=CORUN_API_TOKEN)
            retry_job = _request(f'{CORUN_API_URL}/jobs/',
                                 payload={'prompt_group_id': str(retry_prompt.get('group_id') or ''),
                                          'definition_id': DEFINITION},
                                 token=CORUN_API_TOKEN)
            _mark(state, 'repairing', repair_job_id=str(retry_job.get('id') or retry_job.get('job_id') or ''),
                  problems=problems[:20])
            _log('repair', username=requested_by, reason='; '.join(problems)[:300],
                 job_id=args.job_id, problems_count=len(problems))
            print(json.dumps({'outcome': 'repair', 'problems': problems}))
            return 0
        _mark(state, 'quarantined', job_id=args.job_id, problems=problems[:20],
              raw_page_group_id=args.page_group_id)
        _log('error', username=requested_by, reason='quarantined: ' + '; '.join(problems)[:280],
             job_id=args.job_id, problems_count=len(problems))
        print(json.dumps({'outcome': 'quarantined', 'problems': problems}))
        return 1

    report = spec.render_report(bundle, artifact)
    verdict = artifact.get('verdict', 'ok')
    found = spec.issue_set(artifact)
    last = state.get('last_registered')
    changed = found != last and not (verdict == 'ok' and (last is None or last.get('verdict') == 'ok'))
    now = timezone.now().isoformat(timespec='seconds')
    latest = {'at': now, 'verdict': verdict, 'floor': (bundle.get('floor') or {}).get('verdict'),
              'narration': artifact.get('narration', ''), 'issue_set': found,
              'report': report, 'structured': artifact, 'job_id': args.job_id,
              'bundle': bundle.get('artifact'), 'registered': changed}
    get_product(PRODUCT_KEY, lambda: latest, ttl_seconds=PRODUCT_TTL_S, refresh=True)

    page_group_id = ''
    if changed:
        result = _register_ai_assessment_sync(
            subject_type='live_channel', subject_key='epicprod-live', assessment=report,
            username='live-watch', ai='corun-job', subject_label='epicprod-live channel',
            subject_url=CHANNEL_URL,
            title=f"Live watch: {verdict}"
                  f"{' — ' + ', '.join(found['real_problems'][:3]) if found['real_problems'] else ''}",
            data={'assessment_kind': 'live_watch', 'origin': 'harness',
                  'schema_version': spec.SCHEMA_VERSION, 'verdict': verdict,
                  'narration': artifact.get('narration', '')[:1000], 'structured': artifact,
                  'prompt_group_id': args.prompt_group_id, 'job_id': args.job_id,
                  'bundle': bundle.get('artifact')})
        if not result.get('success'):
            _mark(state, 'registration_failed', job_id=args.job_id, reason=str(result.get('error'))[:300])
            _log('error', username=requested_by, reason=f"registration failed: {result.get('error')}")
            print(json.dumps({'outcome': 'registration_failed', 'error': result.get('error')}))
            return 1
        page_group_id = result.get('corun_page_group_id') or ''
        state['last_registered'] = found
        state['last_registered_at'] = now
    state['last'] = {'at': now, 'verdict': verdict, 'issue_set': found, 'registered': changed}
    _mark(state, 'accepted', job_id=args.job_id, accepted_at=now, page_group_id=page_group_id)
    _log('ok', notify=changed, live=(verdict == 'alarm' and changed), username=requested_by,
         duration_ms=int((time.monotonic() - t0) * 1000), severity=SEVERITY.get(verdict, 'info'),
         verdict=verdict, narration=artifact.get('narration', '')[:600],
         summary=artifact.get('narration', '')[:600], registered=changed,
         real_problems=found['real_problems'], noise=found['noise'],
         corun_page_group_id=page_group_id)
    print(json.dumps({'outcome': 'accepted', 'verdict': verdict, 'registered': changed,
                      'issue_set': found}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
