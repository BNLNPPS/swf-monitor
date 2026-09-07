#!/usr/bin/env python3
"""content-accept.py — accept a sample's content as one operator action.

The production-ops agent's doer for content-validation acceptance
(swf-epicprod docs/EPICPROD_VALIDATION.md, Content validation). The
production record states which file is the delivered output of each work
unit and how many events it carries; the JLab dataset states what it
holds. Acceptance applies the reconciliation's corrections to one dataset:
files that do not belong are detached from the dataset, the delivered
output of each work unit is affirmed, and the sample's delivered event
count becomes the sum of recorded counts. Acceptance never deletes: a
detached file keeps its replica and expires under its own lifetime, so a
mistaken acceptance costs storage and not data.

One run, one dataset:

1. Reconcile live, so a stale finding never drives a detach.
2. Refuse the two cases the reconciliation refuses: a file whose event
   count was never recorded, and a work unit with more than one file in
   delivered status. Refusals come back with their reasons (exit 3).
3. Detach the files that do not belong, through the production account's
   client — the identity every epicprod registration path uses.
4. Verify the dataset listing no longer holds them.
5. Post the acceptance to PCS, the single writer of the record, as the
   operator who asked; then store the fresh finding where the page reads it.

Django-bootstrap standalone script — also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/content-accept.py \
        --task <composed name> --dataset <dataset name> [--owner <user>] [--dry-run]

The last stdout line is a JSON summary; progress goes to stderr.
Exit codes: 0 ok · 2 no such task or dataset · 3 refused · 4 the record
write failed · 5 proxy unusable · 6 Rucio unreachable · 7 detach not verified.
"""
import argparse
import importlib.util
import json
import os
import sys
import urllib.parse
import urllib.request

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

# The credential and the client are the EVGEN registration doer's, as the
# registrar's are, so every writer into the JLab catalog shares one identity.
_spec = importlib.util.spec_from_file_location(
    'register_evgen_rucio', os.path.join(THIS_DIR, 'register-evgen-rucio.py'))
_evgen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_evgen)
# The finding's store is the validation doer's, so the page reads one key.
_spec_cv = importlib.util.spec_from_file_location(
    'content_validate', os.path.join(THIS_DIR, 'content-validate.py'))
_cv = importlib.util.module_from_spec(_spec_cv)
_spec_cv.loader.exec_module(_cv)

RUCIO_SCOPE = _evgen.RUCIO_SCOPE
EXIT_NOT_FOUND, EXIT_REFUSED, EXIT_RECORD, EXIT_PROXY, EXIT_RUCIO, EXIT_VERIFY = 2, 3, 4, 5, 6, 7
DETACH_BATCH = 100


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _record_acceptance(base, owner, token, task_name, body):
    """POST the acceptance to PCS as the operator; the endpoint is the one
    writer of the record (record-content-acceptance)."""
    query = urllib.parse.urlencode({'name': task_name})
    url = f"{base.rstrip('/')}/pcs/api/prod-tasks/record-content-acceptance/?{query}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST')
    req.add_header('Content-Type', 'application/json')
    if owner:
        req.add_header('X-Remote-User', owner)
    if token:
        req.add_header('Authorization', f'Token {token}')
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', required=True, help='the task by composed name')
    parser.add_argument('--dataset', required=True,
                        help='the dataset name the finding named (scope optional)')
    parser.add_argument('--owner', default='',
                        help='the operator accepting; sent as X-Remote-User')
    parser.add_argument('--swf-monitor-url',
                        default=os.environ.get('SWF_MONITOR_URL', '').rstrip('/'))
    parser.add_argument('--token',
                        default=os.environ.get('SWFMON_TOKEN')
                        or os.environ.get('SWF_MONITOR_TOKEN', ''))
    parser.add_argument('--dry-run', action='store_true',
                        help='reconcile and plan; detach nothing, record nothing')
    args = parser.parse_args()

    from pcs import content_validation

    summary = {'task': args.task, 'dataset': args.dataset, 'dry_run': bool(args.dry_run),
               'refusals': [], 'detached': [], 'affirmed': 0, 'delivered_events': 0}

    tasks = _cv.tasks_to_check(None, name=args.task)
    if not tasks:
        summary['error'] = f'no task named {args.task}'
        print(json.dumps(summary))
        return EXIT_NOT_FOUND
    task = tasks[0]
    dataset = args.dataset.split(':', 1)[-1]
    if dataset not in content_validation.datasets_of(task):
        summary['error'] = f'the record names no dataset {dataset} for this task'
        print(json.dumps(summary))
        return EXIT_NOT_FOUND

    finding = content_validation.reconcile(task, dataset_did=dataset)
    plan = content_validation.acceptance_plan(finding)
    summary['refusals'] = plan['refusals']
    summary['affirmed'] = plan['affirm']
    summary['delivered_events'] = plan['delivered_events']
    summary['checked_at'] = finding.get('checked_at', '')
    if not plan['acceptable']:
        for reason in plan['refusals']:
            _log(f'REFUSED: {reason}')
        print(json.dumps(summary))
        return EXIT_REFUSED
    _log(f"{finding['task']} {dataset}: {plan['affirm']} files affirmed, "
         f"{plan['delivered_events']} events, {len(plan['detach'])} to detach")
    if args.dry_run:
        summary['detached'] = plan['detach']
        print(json.dumps(summary))
        return 0

    if plan['detach']:
        try:
            proxy, summary['proxy'] = _evgen.resolve_proxy()
        except _evgen.DoerError as e:
            summary['error'] = e.msg if hasattr(e, 'msg') else str(e)
            print(json.dumps(summary))
            return EXIT_PROXY
        try:
            client = _evgen.rucio_client(proxy)
        except _evgen.DoerError as e:
            summary['error'] = e.msg if hasattr(e, 'msg') else str(e)
            print(json.dumps(summary))
            return EXIT_RUCIO
        for start in range(0, len(plan['detach']), DETACH_BATCH):
            batch = plan['detach'][start:start + DETACH_BATCH]
            try:
                client.detach_dids(scope=RUCIO_SCOPE, name=dataset,
                                   dids=[{'scope': RUCIO_SCOPE, 'name': d} for d in batch])
            except Exception as e:                            # noqa: BLE001
                summary['error'] = f'detach_dids: {e}'
                _log(f'ERROR: detach failed: {e}')
                print(json.dumps(summary))
                return EXIT_RUCIO
            summary['detached'].extend(batch)
            _log(f'detached {len(batch)} file(s) from {dataset}')
        # The catalog is asked what the dataset holds now, never assumed.
        try:
            held = content_validation._dataset_files(dataset)
        except Exception as e:                                # noqa: BLE001
            summary['error'] = f'the dataset could not be re-read: {e}'
            print(json.dumps(summary))
            return EXIT_VERIFY
        still = [d for d in plan['detach'] if d in held]
        if still:
            summary['error'] = f'{len(still)} file(s) still attached after detach'
            summary['still_attached'] = still[:20]
            print(json.dumps(summary))
            return EXIT_VERIFY

    body = {
        'dataset': dataset,
        'delivered_events': plan['delivered_events'],
        'units': finding.get('units', 0),
        'affirmed': plan['affirm'],
        'detached': summary['detached'],
        'checked_at': finding.get('checked_at', ''),
    }
    try:
        _record_acceptance(args.swf_monitor_url, args.owner, args.token,
                           finding['task'], body)
    except Exception as e:                                    # noqa: BLE001
        # The detach stands; the record does not. Loud, with what to re-record.
        summary['error'] = f'record-content-acceptance failed: {e}'
        summary['unrecorded'] = body
        _log(f'ERROR: acceptance not recorded: {e}')
        print(json.dumps(summary))
        return EXIT_RECORD
    summary['recorded'] = True

    # The fresh finding, so the page shows the dataset as it now stands.
    try:
        _cv.store(task, content_validation.reconcile_all(task))
    except Exception as e:                                    # noqa: BLE001
        _log(f'WARNING: fresh finding not stored: {e}')

    print(json.dumps(summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
