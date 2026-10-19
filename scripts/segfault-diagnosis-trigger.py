#!/usr/bin/env python3
"""segfault-diagnosis-trigger.py — submit a crash signature's LLM study.

Stage 5 of swf-epicprod docs/SEGFAULT_DIAGNOSIS.md, on the assessment
harness pattern (EPICPROD_ASSESSMENTS_V1.md): the bundle is assembled
deterministically from the signature record (the signature, its trace,
the representative and reproduction job records, the configuration with
the image the task ran, the reproduction package README when one is
built), stored as a hidden corun bundle Page in the segfault bundle
section, and submitted as the run's prompt content with the
``segfault_diagnosis`` definition (POST /prompts/, then POST /jobs/).
The run's job id is recorded on the signature (``data['diagnosis']``)
and a ``segfault_diagnosis_triggered`` action is logged either way.

Django-bootstrap standalone script, run by the ops agent for the
Diagnose action and for the automatic trigger, or by hand::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/segfault-diagnosis-trigger.py --key exit139:task39623 [--dry-run]

Environment: SWF_MONITOR_URL, CORUN_API_URL (or CORUN_BASE_URL),
CORUN_API_TOKEN, CORUN_SEGFAULT_SECTION, CORUN_SEGFAULT_BUNDLE_SECTION,
CORUN_SEGFAULT_DEFINITION (printed by swf_epicprod.segfault.bootstrap).
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

from django.utils import timezone  # noqa: E402

from monitor_app.epicprod_logging import log_epicprod_action  # noqa: E402
from monitor_app.models import CrashSignature, EpicProdJob  # noqa: E402
from monitor_app.segfaults import signature_detail  # noqa: E402
from swf_epicprod.assessment.trigger import (CORUN_API_TOKEN, CORUN_API_URL,  # noqa: E402
                                             CORUN_WEB_URL, _request)
from swf_epicprod.segfault import spec  # noqa: E402

SECTION = os.environ.get('CORUN_SEGFAULT_SECTION', spec.DEFAULT_SECTION)
BUNDLE_SECTION = os.environ.get('CORUN_SEGFAULT_BUNDLE_SECTION', spec.DEFAULT_BUNDLE_SECTION)
DEFINITION = os.environ.get('CORUN_SEGFAULT_DEFINITION', '')
MONITOR_URL = os.environ.get('SWF_MONITOR_URL', '').rstrip('/')
log = logging.getLogger('segfault-diagnosis-trigger')


def _job_record(pandaid):
    job = EpicProdJob.objects.filter(pandaid=int(pandaid)).only('pandaid', 'jeditaskid', 'seq_number', 'data').first()
    if job is None:
        return None
    return {'pandaid': job.pandaid, 'jeditaskid': job.jeditaskid, 'seq_number': job.seq_number,
            'crash': (job.data or {}).get('crash') or {}}


def assemble(key):
    """The evidence bundle for one signature, deterministic."""
    detail = signature_detail(key, jobs_limit=20)
    if detail is None:
        raise ValueError(f'no crash signature {key}')
    sig = CrashSignature.objects.get(key=key)
    trace = detail.get('trace') or {}
    representative = _job_record(trace['source_pandaid']) if trace.get('source_pandaid') else None
    reproductions = []
    for e in detail.get('reproduction') or []:
        entry = dict(e)
        if e.get('canary_pandaid'):
            entry['canary_job'] = _job_record(e['canary_pandaid'])
        reproductions.append(entry)
    readme = ''
    path = (sig.package or {}).get('path')
    if path and os.path.isfile(os.path.join(path, 'README.md')):
        with open(os.path.join(path, 'README.md')) as fh:
            readme = fh.read()[:20000]
    manifest = [{'source': 'signature record', 'ok': True},
                {'source': 'trace', 'ok': trace.get('trace_status') == 'found'},
                {'source': 'representative job', 'ok': representative is not None},
                {'source': 'reproductions', 'ok': bool(reproductions)},
                {'source': 'package README', 'ok': bool(readme)}]
    return {
        'schema': 'epicprod-segfault-bundle/1',
        'generated_at': datetime.now(dt_timezone.utc).isoformat(timespec='seconds'),
        'params': {'key': key},
        'signature': {k: v for k, v in detail.items()
                      if k not in ('jobs', 'trace', 'reproduction', 'data')},
        'trace': trace,
        'representative_job': representative,
        'crashed_jobs': detail.get('jobs') or [],
        'reproductions': reproductions,
        'package_readme': readme,
        'monitor_urls': {
            'signature': f'{MONITOR_URL}/panda/segfaults/{key}/',
            'catalog': f'{MONITOR_URL}/panda/segfaults/',
        },
        'manifest': manifest,
        'degraded': not all(m['ok'] for m in manifest[:2]),
    }


def persist_bundle(bundle):
    key = bundle['params']['key']
    page = _request(
        f'{CORUN_API_URL}/pages/',
        payload={
            'section': BUNDLE_SECTION,
            'title': f"ePIC segfault diagnosis evidence {key} — {bundle['generated_at']}",
            'content': '```json\n' + json.dumps(bundle, indent=1, default=str) + '\n```\n',
            'data': {'ui_visible': False, 'artifact_type': 'segfault_diagnosis_evidence_bundle',
                     'source_system': 'epicprod', 'subject_type': 'crash_signature',
                     'subject_key': key, 'generated_at': bundle['generated_at'],
                     'evidence_bundle': bundle},
            'tags': ['evidence-bundle', 'epicprod', 'segfault'],
        },
        token=CORUN_API_TOKEN)
    bundle_id = str(page.get('group_id') or '')
    if not bundle_id:
        raise RuntimeError(f'corun bundle Page response contained no group id: {page!r}')
    bundle['artifact'] = {'type': 'corun_page', 'id': bundle_id,
                          'url': f'{CORUN_WEB_URL}/page/{bundle_id}/', 'section': BUNDLE_SECTION}
    return bundle_id


def submit(key, requested_by='', dry_run=False):
    bundle = assemble(key)
    if dry_run:
        print(json.dumps({'key': key, 'dry_run': True, 'degraded': bundle['degraded'],
                          'manifest': bundle['manifest'],
                          'bytes': len(json.dumps(bundle, default=str))}))
        return 0
    if not (CORUN_API_URL and CORUN_API_TOKEN and DEFINITION):
        raise RuntimeError('CORUN_API_URL, CORUN_API_TOKEN and CORUN_SEGFAULT_DEFINITION are required')
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
    sig = CrashSignature.objects.get(key=key)
    data = dict(sig.data or {})
    data['diagnosis'] = {'job_id': job_id, 'prompt_group_id': str(prompt.get('group_id') or ''),
                         'bundle_id': bundle_id,
                         'submitted_at': timezone.now().isoformat(timespec='seconds'),
                         'requested_by': requested_by, 'state': 'submitted'}
    sig.data = data
    sig.save(update_fields=['data', 'updated_at'])
    return {'key': key, 'job_id': job_id, 'prompt_group_id': data['diagnosis']['prompt_group_id'],
            'bundle_id': bundle_id, 'degraded': bundle['degraded']}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--key', required=True)
    ap.add_argument('--requested-by', default='')
    ap.add_argument('--dry-run', action='store_true', help='assemble and report, submit nothing')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    t0 = time.monotonic()
    try:
        result = submit(args.key, args.requested_by, args.dry_run)
    except Exception as e:                                    # noqa: BLE001
        log.exception('trigger failed')
        log_epicprod_action('segfault-diagnosis', 'segfault_diagnosis_triggered', outcome='error',
                            subject_type='crash_signature', subject_key=args.key,
                            username=args.requested_by, sublevel='normal', live_default=True,
                            level=logging.ERROR, duration_ms=int((time.monotonic() - t0) * 1000),
                            message=f'segfault diagnosis trigger {args.key} failed: {str(e)[:200]}',
                            reason=str(e)[:300])
        print(json.dumps({'key': args.key, 'error': str(e)[:300]}))
        return 1
    if result == 0:
        return 0
    log_epicprod_action('segfault-diagnosis', 'segfault_diagnosis_triggered', outcome='ok',
                        subject_type='crash_signature', subject_key=args.key,
                        username=args.requested_by, sublevel='normal', live_default=True,
                        duration_ms=int((time.monotonic() - t0) * 1000),
                        message=f"segfault diagnosis {args.key} submitted: corun job {result['job_id']}",
                        job_id=result['job_id'], degraded=result['degraded'])
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
