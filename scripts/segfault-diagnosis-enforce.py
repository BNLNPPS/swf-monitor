#!/usr/bin/env python3
"""segfault-diagnosis-enforce.py — accept a segfault diagnosis run's result.

Stage 5 of swf-epicprod docs/SEGFAULT_DIAGNOSIS.md, the enforcement end
of the harness (EPICPROD_ASSESSMENTS_V1.md): on the corun completion
callback the ops agent runs this with the job, the prompt group and the
result page. The artifact is extracted and validated against the schema
and the verdict floor; on failure one bounded repair run receives the
exact problems and the prior output, and a second failure quarantines
the artifact (the raw output retained, nothing registered as a
diagnosis). A valid artifact is rendered with the bundle's facts and
registered as an assessment on the signature (subject type
``crash_signature``), the signature's status becomes ``diagnosed``
(``handed_off`` when the operator action is hand_off, with the handoff
text stored) and its verdict the classification and action; the
``segfault_diagnosis`` action carries the classification as its
severity for notice routing.

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/segfault-diagnosis-enforce.py \\
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

from monitor_app.epicprod_logging import log_epicprod_action  # noqa: E402
from monitor_app.mcp.ai_content import _register_ai_assessment_sync  # noqa: E402
from monitor_app.models import CrashSignature  # noqa: E402
from swf_epicprod.assessment.bundle import _get  # noqa: E402
from swf_epicprod.assessment.trigger import (CORUN_API_TOKEN, CORUN_API_URL,  # noqa: E402
                                             _request)
from swf_epicprod.segfault import spec  # noqa: E402

SECTION = os.environ.get('CORUN_SEGFAULT_SECTION', spec.DEFAULT_SECTION)
DEFINITION = os.environ.get('CORUN_SEGFAULT_DEFINITION', '')
SEVERITY = {'software_defect': 'warning', 'configuration': 'warning',
            'event_shaped': 'info', 'platform': 'info', 'unresolved': 'info'}
log = logging.getLogger('segfault-diagnosis-enforce')


def _log(key, outcome, username='', **counts):
    log_epicprod_action('segfault-diagnosis', 'segfault_diagnosis', outcome=outcome,
                        subject_type='crash_signature', subject_key=key, username=username,
                        sublevel='normal', live_default=True,
                        level=logging.ERROR if outcome == 'error' else logging.INFO,
                        url=f'/panda/segfaults/{key}/', **counts)


def _mark(sig, state, **fields):
    data = dict(sig.data or {})
    diag = dict(data.get('diagnosis') or {})
    diag['state'] = state
    diag.update(fields)
    data['diagnosis'] = diag
    sig.data = data
    sig.save(update_fields=['data', 'updated_at'])


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

    prompt = _get(f'{CORUN_API_URL}/prompts/{args.prompt_group_id}/', token=CORUN_API_TOKEN)
    try:
        submitted = json.loads(prompt.get('content') or '{}')
    except json.JSONDecodeError:
        submitted = {}
    bundle = submitted.get('bundle') or {}
    key = (bundle.get('params') or {}).get('key') or ''
    sig = CrashSignature.objects.filter(key=key).first() if key else None
    if sig is None:
        _log(key or 'unknown', 'error', reason=f'no signature for prompt {args.prompt_group_id}')
        print(json.dumps({'error': f'no signature for prompt {args.prompt_group_id}'}))
        return 1
    requested_by = ((sig.data or {}).get('diagnosis') or {}).get('requested_by') or ''

    if args.status != 'completed':
        _mark(sig, 'failed', job_id=args.job_id, reason=f'run ended {args.status}')
        _log(key, 'error', requested_by, reason=f'corun run {args.job_id} ended {args.status}')
        print(json.dumps({'key': key, 'outcome': 'failed', 'status': args.status}))
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
            _mark(sig, 'repairing', repair_job_id=str(retry_job.get('id') or retry_job.get('job_id') or ''),
                  problems=problems[:20])
            _log(key, 'repair', requested_by, reason='; '.join(problems)[:300],
                 job_id=args.job_id, problems_count=len(problems))
            print(json.dumps({'key': key, 'outcome': 'repair', 'problems': problems}))
            return 0
        _mark(sig, 'quarantined', job_id=args.job_id, problems=problems[:20],
              raw_page_group_id=args.page_group_id)
        _log(key, 'error', requested_by, reason='quarantined: ' + '; '.join(problems)[:280],
             job_id=args.job_id, problems_count=len(problems))
        print(json.dumps({'key': key, 'outcome': 'quarantined', 'problems': problems}))
        return 1

    report = spec.render_report(bundle, artifact)
    classification = artifact.get('classification', 'unresolved')
    action = artifact.get('operator_action', 'none')
    verdict = (f"{classification} ({artifact.get('confidence', '')}): "
               f"{action} — {artifact.get('action_detail', '')}").strip()
    result = _register_ai_assessment_sync(
        subject_type='crash_signature', subject_key=key, assessment=report,
        username='segfault-diagnosis', ai='corun-job', subject_label='', subject_url='',
        title=f'Segfault diagnosis {key}: {classification}, {action}',
        data={'assessment_kind': 'segfault_diagnosis', 'origin': 'harness',
              'schema_version': spec.SCHEMA_VERSION, 'verdict': classification,
              'narration': ' '.join(artifact.get('diagnosis') or [])[:1000],
              'structured': artifact, 'prompt_group_id': args.prompt_group_id,
              'job_id': args.job_id, 'bundle': bundle.get('artifact')})
    if not result.get('success'):
        _mark(sig, 'registration_failed', job_id=args.job_id, reason=str(result.get('error'))[:300])
        _log(key, 'error', requested_by, reason=f"registration failed: {result.get('error')}")
        print(json.dumps({'key': key, 'outcome': 'registration_failed', 'error': result.get('error')}))
        return 1

    sig = CrashSignature.objects.get(key=key)
    sig.verdict = verdict
    ids = list((sig.data or {}).get('corun_page_group_ids') or [])
    if result.get('corun_page_group_id') and result['corun_page_group_id'] not in ids:
        ids.append(result['corun_page_group_id'])
    sig.assessment_ids = ids
    if action == 'hand_off':
        sig.status = 'handed_off'
        package = dict(sig.package or {})
        package['handoff_text'] = artifact.get('handoff_text', '')
        sig.package = package
    elif sig.status not in ('fixed', 'accepted'):
        sig.status = 'diagnosed'
    sig.save(update_fields=['verdict', 'assessment_ids', 'status', 'package', 'updated_at'])
    _mark(sig, 'diagnosed', job_id=args.job_id, classification=classification,
          operator_action=action, page_group_id=result.get('corun_page_group_id'),
          diagnosed_at=timezone.now().isoformat(timespec='seconds'))
    _log(key, 'ok', requested_by, duration_ms=int((time.monotonic() - t0) * 1000),
         severity=SEVERITY.get(classification, 'info'), classification=classification,
         operator_action=action, narration=' '.join(artifact.get('diagnosis') or [])[:600],
         corun_page_group_id=result.get('corun_page_group_id') or '')
    print(json.dumps({'key': key, 'outcome': 'diagnosed', 'classification': classification,
                      'operator_action': action, 'page_group_id': result.get('corun_page_group_id')}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
