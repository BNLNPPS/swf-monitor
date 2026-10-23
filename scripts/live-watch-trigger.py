#!/usr/bin/env python3
"""live-watch-trigger.py — submit one live watch run.

The front end of the live watch (swf-epicprod docs/EPICPROD_ASSESSMENTS.md,
The live watch), on the segfault diagnosis's harness pattern: the bundle is
assembled deterministically (``swf_epicprod.livewatch.evidence``: the
channel's posts, the action record's failure groups and flapping counts,
the publication policy, the mechanical floor), stored as a hidden corun
bundle Page, and submitted as the run's prompt content with the
``live_watch`` definition (POST /prompts/, then POST /jobs/). The run is
recorded in PersistentState under ``live_watch`` and a quiet
``live_watch_triggered`` action is logged either way.

Run by the ops agent's ``live_watch`` handler (every four hours by cron
enqueue), or by hand::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/live-watch-trigger.py [--dry-run]

Environment: CORUN_API_URL (or CORUN_BASE_URL), CORUN_API_TOKEN,
CORUN_LIVEWATCH_SECTION, CORUN_LIVEWATCH_BUNDLE_SECTION,
CORUN_LIVEWATCH_DEFINITION (printed by swf_epicprod.livewatch.bootstrap),
MATTERMOST_TOKEN for the channel read.
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone as dt_timezone

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from monitor_app.epicprod_logging import log_epicprod_action  # noqa: E402
from monitor_app.models import PersistentState  # noqa: E402
from swf_epicprod.assessment.trigger import (CORUN_API_TOKEN, CORUN_API_URL,  # noqa: E402
                                             CORUN_WEB_URL, _request)
from swf_epicprod.livewatch import evidence, spec  # noqa: E402

SECTION = os.environ.get('CORUN_LIVEWATCH_SECTION', spec.DEFAULT_SECTION)
BUNDLE_SECTION = os.environ.get('CORUN_LIVEWATCH_BUNDLE_SECTION', spec.DEFAULT_BUNDLE_SECTION)
DEFINITION = os.environ.get('CORUN_LIVEWATCH_DEFINITION', '')
log = logging.getLogger('live-watch-trigger')


def persist_bundle(bundle):
    page = _request(
        f'{CORUN_API_URL}/pages/',
        payload={
            'section': BUNDLE_SECTION,
            'title': f"ePIC live watch evidence — {bundle['generated_at']}",
            'content': '```json\n' + json.dumps(bundle, indent=1, default=str) + '\n```\n',
            'data': {'ui_visible': False, 'artifact_type': 'live_watch_evidence_bundle',
                     'source_system': 'epicprod', 'subject_type': 'live_channel',
                     'subject_key': 'epicprod-live', 'generated_at': bundle['generated_at'],
                     'evidence_bundle': bundle},
            'tags': ['evidence-bundle', 'epicprod', 'live-watch'],
        },
        token=CORUN_API_TOKEN)
    bundle_id = str(page.get('group_id') or '')
    if not bundle_id:
        raise RuntimeError(f'corun bundle Page response contained no group id: {page!r}')
    bundle['artifact'] = {'type': 'corun_page', 'id': bundle_id,
                          'url': f'{CORUN_WEB_URL}/page/{bundle_id}/', 'section': BUNDLE_SECTION}
    return bundle_id


def submit(requested_by='', dry_run=False):
    bundle = evidence.assemble()
    if dry_run:
        print(json.dumps({'dry_run': True, 'floor': bundle['floor'], 'degraded': bundle['degraded'],
                          'posts': len(bundle['channel'].get('posts') or []),
                          'groups': len(bundle['failure_groups']),
                          'bytes': len(json.dumps(bundle, default=str))}))
        return 0
    if not (CORUN_API_URL and CORUN_API_TOKEN and DEFINITION):
        raise RuntimeError('CORUN_API_URL, CORUN_API_TOKEN and CORUN_LIVEWATCH_DEFINITION are required')
    bundle_id = persist_bundle(bundle)
    content = json.dumps({
        'submitted_at': datetime.now(dt_timezone.utc).isoformat(timespec='seconds'),
        'bundle': bundle,
    }, default=str)
    prompt = _request(f'{CORUN_API_URL}/prompts/',
                      payload={'section': SECTION, 'content': content, 'definition_id': DEFINITION},
                      token=CORUN_API_TOKEN)
    job = _request(f'{CORUN_API_URL}/jobs/',
                   payload={'prompt_group_id': str(prompt.get('group_id') or ''),
                            'definition_id': DEFINITION},
                   token=CORUN_API_TOKEN)
    job_id = str(job.get('id') or job.get('job_id') or '')
    if not job_id:
        raise RuntimeError(f'corun job response contained no job id: {job!r}')
    state = dict(PersistentState.get_state().get('live_watch') or {})
    state['current'] = {'job_id': job_id, 'prompt_group_id': str(prompt.get('group_id') or ''),
                        'bundle_id': bundle_id, 'submitted_at': bundle['generated_at'],
                        'requested_by': requested_by, 'state': 'submitted',
                        'floor': bundle['floor']['verdict']}
    PersistentState.update_state({'live_watch': state})
    return {'job_id': job_id, 'bundle_id': bundle_id, 'floor': bundle['floor']['verdict'],
            'degraded': bundle['degraded']}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--requested-by', default='')
    ap.add_argument('--dry-run', action='store_true', help='assemble and report, submit nothing')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    t0 = time.monotonic()
    try:
        result = submit(args.requested_by, args.dry_run)
    except Exception as e:                                    # noqa: BLE001
        log.exception('trigger failed')
        # A failed trigger is a watch that did not run: visible in the channel.
        log_epicprod_action('live-watch', 'live_watch_triggered', outcome='error',
                            username=args.requested_by, sublevel='normal', live_default=True,
                            level=logging.ERROR, duration_ms=int((time.monotonic() - t0) * 1000),
                            message=f'live watch trigger failed: {str(e)[:200]}',
                            reason=str(e)[:300])
        print(json.dumps({'error': str(e)[:300]}))
        return 1
    if result == 0:
        return 0
    log_epicprod_action('live-watch', 'live_watch_triggered', outcome='ok',
                        username=args.requested_by, sublevel='low', live_default=False,
                        duration_ms=int((time.monotonic() - t0) * 1000),
                        message=f"live watch submitted: corun job {result['job_id']}, floor {result['floor']}",
                        job_id=result['job_id'], floor=result['floor'], degraded=result['degraded'])
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
