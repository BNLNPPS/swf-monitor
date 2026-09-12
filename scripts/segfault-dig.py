#!/usr/bin/env python3
"""segfault-dig.py — the trace of a crash signature from its payload log.

Stage 3 of swf-epicprod docs/SEGFAULT_DIAGNOSIS.md. For a signature of
the segfault catalog, this reads one representative crashed job's
payload log and records the crash trace on the signature: the job is
the one with the median time to death (or the one named), its log
tarball is resolved from the PanDA record (the job's own file row while
``filestable4`` holds it, else the task's log dataset contents row) and
fetched through the payload-log doer (``cache-payload-log.py``, the
Rucio proxy and xrootd, cached under ``$SWF_TMP_DIR/panda-logs``), and
``trace_extract`` reads the cached ``payload.stdout`` and
``payload.stderr`` for the backtrace. Verified 2026-09-11: eicrecon
prints JANA2's backtrace into payload.stdout (jobs 994773 and 2723039);
the storm's tarballs (BNL_OSG_EPIC_PROD_1) have no replica, which the
signature records as ``log_unavailable`` with the reason.

Two record-level signatures with the same crashing frame merge into one
trace-level entry; the record-level entries stay as its members.

Django-bootstrap standalone script, run by the ops agent (the Dig
action, and the nightly automatic dig) and usable by hand under the
account that holds the Rucio proxy::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/segfault-dig.py --key exit139:task39623
    ... --key K --pandaid P          # a chosen job instead of the median one
    ... --auto 10                    # the signatures never dug, largest first

The last stdout line is a JSON summary; progress and errors go to stderr.
"""
import argparse
import json
import logging
import os
import subprocess
import sys
import time

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from monitor_app.epicprod_inventory import trace_extract  # noqa: E402
from monitor_app.models import CrashSignature  # noqa: E402
from monitor_app.segfaults import (dig_candidates, record_trace, representative,  # noqa: E402
                                   resolve_log_did)

DOER = os.path.join(THIS_DIR, 'cache-payload-log.py')
SWF_TMP_DIR = os.environ.get('SWF_TMP_DIR', '/data/swf-tmp')
DOER_TIMEOUT = int(os.environ.get('SEGFAULT_DIG_DOER_TIMEOUT', '300'))
MEMBERS = ('payload.stdout', 'payload.stderr')

log = logging.getLogger('segfault-dig')


def fetch_log(scope, lfn, jeditaskid, pandaid):
    """The cached log directory after the doer ran, or (None, reason)."""
    jobdir = os.path.join(SWF_TMP_DIR, 'panda-logs', str(jeditaskid), str(pandaid))
    if os.path.exists(os.path.join(jobdir, '.done')):
        return jobdir, ''
    cmd = [sys.executable, DOER, '--scope', scope, '--lfn', lfn,
           '--jeditaskid', str(jeditaskid), '--pandaid', str(pandaid)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=DOER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, f'log fetch timed out after {DOER_TIMEOUT}s'
    for line in (p.stderr or '').splitlines():
        log.info('  cache-payload-log: %s', line[:200])
    if p.returncode != 0 or not os.path.exists(os.path.join(jobdir, '.done')):
        errs = [l for l in (p.stderr or '').splitlines() if l.startswith('ERROR')]
        return None, (errs[-1] if errs else f'doer exited {p.returncode}')[:300]
    return jobdir, ''


def metatable_note(pandaid):
    """The payload report's note of a finished job, from the PanDA metatable
    (the pilot lifts jobReport.json there for finished jobs only; the
    dispatcher carries the payload report under ``payload``). '' when the
    job has none."""
    from django.db import connections
    from monitor_app.panda.constants import PANDA_SCHEMA
    try:
        with connections['panda'].cursor() as cur:
            cur.execute(f'SELECT "metadata" FROM "{PANDA_SCHEMA}"."metatable" WHERE "pandaid" = %s',
                        [int(pandaid)])
            row = cur.fetchone()
    except Exception as e:                                    # noqa: BLE001
        log.error('job %s: metatable read failed: %s', pandaid, e)
        return ''
    if not row or not row[0]:
        return ''
    raw = row[0]
    try:
        meta = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError) as e:
        log.error('job %s: metatable metadata unparsable: %s', pandaid, e)
        return ''
    payload = meta.get('payload') if isinstance(meta, dict) else None
    return str((payload or {}).get('note') or '') if isinstance(payload, dict) else ''


def dig(sig, pandaid=None):
    """One dig; returns the summary dict for the signature."""
    if pandaid is None:
        rep = representative(sig)
        if rep is None:
            return {'key': sig.key, 'trace_status': 'log_unavailable',
                    'reason': 'no crashed job on record'}
        pandaid, jeditaskid = rep
    else:
        from monitor_app.models import EpicProdJob
        job = EpicProdJob.objects.filter(pandaid=int(pandaid)).only('jeditaskid').first()
        jeditaskid = job.jeditaskid if job else None
        if not jeditaskid:
            return {'key': sig.key, 'trace_status': 'log_unavailable',
                    'reason': f'job {pandaid} is not in the crash inventory'}
    sig.status = 'digging' if sig.status == 'new' else sig.status
    sig.save(update_fields=['status', 'updated_at'])
    log.info('%s: representative job %s (task %s)', sig.key, pandaid, jeditaskid)
    # Payload 0.12 and later send the crashing stage's log tail in the
    # report's note (SEGFAULT_DIAGNOSIS.md, Traces going forward); the
    # sweep files it beside the job, so no tarball is fetched.
    from monitor_app.models import EpicProdJob as _Job
    filed = (_Job.objects.filter(pandaid=int(pandaid)).only('data').first() or _Job()).data or {}
    note = (((filed.get('payload_report') or {}).get('report') or {}).get('note') or '')
    source = 'payload_report'
    if not note.startswith('crash:'):
        # A job that finished at the server (a reproduction on the reference
        # queue, which carries no log dataset) has its report in the PanDA
        # metatable, not in the sweep's filing.
        note, source = metatable_note(pandaid), 'metatable'
    if note.startswith('crash:'):
        result = trace_extract([note])
        if result['trace_status'] == 'found':
            log.info('%s: trace read from the %s payload report of job %s', sig.key, source, pandaid)
            merged = record_trace(sig, result, pandaid, '')
            return {'key': sig.key, 'pandaid': pandaid, 'trace_status': 'found',
                    'source': source, 'program': result.get('program'),
                    'stage': result.get('stage'), 'frame': result.get('frame'),
                    'library': result.get('library'),
                    'events_processed': result.get('events_processed'), 'merged_into': merged}
    scope, lfn = resolve_log_did(pandaid, jeditaskid)
    if not lfn:
        reason = 'no log file on the PanDA record'
        record_trace(sig, {'trace_status': 'log_unavailable'}, pandaid, reason)
        return {'key': sig.key, 'pandaid': pandaid, 'trace_status': 'log_unavailable',
                'reason': reason}
    jobdir, reason = fetch_log(scope, lfn, jeditaskid, pandaid)
    if jobdir is None:
        record_trace(sig, {'trace_status': 'log_unavailable'}, pandaid, reason)
        return {'key': sig.key, 'pandaid': pandaid, 'trace_status': 'log_unavailable',
                'reason': reason}
    texts = []
    for member in MEMBERS:
        path = os.path.join(jobdir, member)
        if os.path.exists(path):
            with open(path, errors='replace') as fh:
                texts.append(fh.read())
    result = trace_extract(texts)
    merged = record_trace(sig, result, pandaid,
                          '' if result['trace_status'] == 'found'
                          else 'no backtrace in the cached payload output')
    return {'key': sig.key, 'pandaid': pandaid, 'trace_status': result['trace_status'],
            'program': result.get('program'), 'stage': result.get('stage'),
            'frame': result.get('frame'), 'library': result.get('library'),
            'events_processed': result.get('events_processed'), 'merged_into': merged}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--key', help='the signature to dig')
    ap.add_argument('--pandaid', type=int, help='the job to read instead of the median one')
    ap.add_argument('--auto', type=int, metavar='N',
                    help='dig the N largest signatures never dug')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    t0 = time.monotonic()
    if args.key:
        sig = CrashSignature.objects.filter(key=args.key).first()
        if sig is None:
            print(json.dumps({'error': f'no crash signature {args.key}'}))
            return 2
        try:
            summary = dig(sig, args.pandaid)
        except Exception as e:                                # noqa: BLE001
            log.exception('dig failed')
            record_trace(sig, {'trace_status': 'log_unavailable'}, args.pandaid or 0,
                         f'dig failed: {str(e)[:200]}')
            print(json.dumps({'key': args.key, 'error': str(e)[:300]}))
            return 1
        summary['seconds'] = round(time.monotonic() - t0, 1)
        print(json.dumps(summary))
        return 0
    if args.auto:
        digs, found, unavailable = [], 0, 0
        for sig in dig_candidates(args.auto):
            try:
                s = dig(sig)
            except Exception as e:                            # noqa: BLE001
                log.exception('dig %s failed', sig.key)
                record_trace(sig, {'trace_status': 'log_unavailable'}, 0,
                             f'dig failed: {str(e)[:200]}')
                s = {'key': sig.key, 'trace_status': 'log_unavailable', 'error': str(e)[:200]}
            digs.append(s)
            found += s.get('trace_status') == 'found'
            unavailable += s.get('trace_status') == 'log_unavailable'
        print(json.dumps({'digs': len(digs), 'found': found, 'log_unavailable': unavailable,
                          'absent': len(digs) - found - unavailable, 'results': digs,
                          'seconds': round(time.monotonic() - t0, 1)}))
        return 0
    ap.error('one of --key or --auto is required')


if __name__ == '__main__':
    sys.exit(main())
