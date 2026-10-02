#!/usr/bin/env python3
"""log-rescue.py — move logs held in the stage-out bucket to their datasets.

A pilot whose log transfer failed holds the log in the devcloud stage-out
bucket under ``logs/<site>/<log dataset>/<lfn>`` and reports ``logHeld=s3`` in
the job metrics (swf-epicprod docs/LOG_STAGEOUT_FALLBACK.md). Objects there
expire after seven days. This pass puts each held log where the job meant it
to go: the queue's log RSE, under its own name and GUID, attached to the log
dataset in BNL Rucio.

One pass:

1. Find the held logs: jobs of the window whose metrics carry ``logHeld=s3``,
   with their log file from the PanDA file table.
2. Skip a log BNL Rucio already holds a replica of.
3. Fetch the object from the bucket and upload it to the queue's log RSE
   (``astorages.pl`` of the queue's schedconfig), registering it under the
   job's GUID and attaching it to the log dataset. A closed log dataset is
   reopened first, as the retry pass does.

A log that will not move keeps its state entry and is tried on later passes,
a bounded number of times. Objects are not deleted; their expiry does that.
A queue whose log storage is the bucket itself (BNL_NPPS_GPU) has nothing to
move.

Django-bootstrap standalone script; also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/log-rescue.py [--hours 168] [--pandaid N] [--dry-run]

The last stdout line is a JSON summary; progress goes to stderr.
Exit codes: 0 ok · 5 no usable credential.
"""
import argparse
import importlib.util
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone as dt_timezone

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from django.db import connections  # noqa: E402

from monitor_app.panda.constants import PANDA_SCHEMA  # noqa: E402

# The BNL Rucio identity and connection of the retry pass, imported rather
# than copied so the two writers into the log datasets cannot drift.
_spec = importlib.util.spec_from_file_location(
    'panda_task_operation', os.path.join(THIS_DIR, 'panda-task-operation.py'))
_op = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_op)

BUCKET = os.environ.get('EPICPROD_STAGEOUT_BUCKET', 'epic-devcloud-stageout')
REGION = os.environ.get('EPICPROD_STAGEOUT_REGION', 'us-east-1')
PROFILE = os.environ.get('EPICPROD_STAGEOUT_AWS_PROFILE', 'epic-stageout')
BUCKET_STORAGE = 'DEV_CLOUD_S3'
DEFAULT_HOURS = 168
STATE_PATH = os.environ.get('LOG_RESCUE_STATE',
                            '/data/wenauseic/swf-delivery/log-rescue-state.json')
MAX_ATTEMPTS = int(os.environ.get('LOG_RESCUE_MAX_ATTEMPTS', 12))


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def held_logs(since, pandaid=None):
    """[(pandaid, site, jeditaskid, lfn, guid, dataset, scope)] of held logs."""
    found = []
    for table in ('jobsarchived4', 'jobsactive4'):
        sql = f"""
            SELECT j."pandaid", j."computingsite", j."jeditaskid",
                   f."lfn", f."guid", f."dataset", f."scope"
            FROM "{PANDA_SCHEMA}"."{table}" j
            JOIN "{PANDA_SCHEMA}"."filestable4" f
              ON f."pandaid" = j."pandaid" AND f."type" = 'log'
            WHERE j."modificationtime" >= %s AND j."jobmetrics" LIKE %s
        """
        args = [since, '%logHeld=s3%']
        if pandaid:
            sql += ' AND j."pandaid" = %s'
            args.append(int(pandaid))
        with connections['panda'].cursor() as cursor:
            cursor.execute(sql, args)
            found.extend(cursor.fetchall())
    return found


def log_storage(site, cache={}):
    """The RSE a queue's pilots write logs to: astorages.pl of its schedconfig."""
    if site not in cache:
        sql = f'SELECT "data" FROM "{PANDA_SCHEMA}"."schedconfig_json" WHERE "panda_queue" = %s'
        with connections['panda'].cursor() as cursor:
            cursor.execute(sql, [site])
            row = cursor.fetchone()
        data = row[0] if row else {}
        if isinstance(data, str):
            data = json.loads(data)
        storages = (data or {}).get('astorages') or {}
        names = storages.get('pl') or storages.get('write_lan') or storages.get('w') or []
        cache[site] = names[0] if names else ''
    return cache[site]


def load_state():
    try:
        with open(STATE_PATH) as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + '.tmp'
    with open(tmp, 'w') as fp:
        json.dump(state, fp, indent=1, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def rucio_clients():
    from rucio.client import Client
    from rucio.client.uploadclient import UploadClient
    client = Client(rucio_host=_op.RUCIO_URL, auth_host=_op.RUCIO_URL, account=_op.RUCIO_ACCOUNT,
                    auth_type='x509_proxy', creds={'client_proxy': _op.X509_PROXY},
                    ca_cert=None, vo=_op.RUCIO_VO)
    client.whoami()
    return client, UploadClient(_client=client)


def has_replica(client, scope, lfn):
    try:
        for rep in client.list_replicas([{'scope': scope, 'name': lfn}]):
            if rep.get('states') and 'AVAILABLE' in rep['states'].values():
                return True
    except Exception as exc:  # DataIdentifierNotFound: not yet registered
        if 'not found' not in str(exc).lower():
            raise
    return False


def ensure_open(client, scope, dataset):
    meta = client.get_metadata(scope=scope, name=dataset)
    if meta.get('is_open') is False:
        client.set_status(scope=scope, name=dataset, open=True)
        client.set_metadata(scope=scope, name=dataset, key='lifetime',
                            value=_op.REOPENED_LIFETIME_DAYS * 86400)
        _log(f'reopened closed log dataset {scope}:{dataset}')


def rescue(client, uploader, s3, entry, dry_run):
    pandaid, site, jeditaskid, lfn, guid, dataset, scope = entry
    rse = log_storage(site)
    key = f'logs/{site}/{dataset}/{lfn}'
    if not rse or rse == BUCKET_STORAGE:
        return 'bucket', f'{site} keeps its logs in the bucket'
    if has_replica(client, scope, lfn):
        return 'done', f'{scope}:{lfn} already at an RSE'
    if dry_run:
        return 'dry_run', f'would move {key} to {rse} and attach to {scope}:{dataset}'
    with tempfile.TemporaryDirectory(prefix='log-rescue-') as tmp:
        path = os.path.join(tmp, lfn)
        s3.download_file(BUCKET, key, path)
        ensure_open(client, scope, dataset)
        uploader.upload([{'path': path, 'rse': rse, 'did_scope': scope, 'did_name': lfn,
                          'guid': guid, 'dataset_scope': scope, 'dataset_name': dataset,
                          'register_after_upload': True}])
    return 'done', f'{key} -> {rse}, attached to {scope}:{dataset}'


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--hours', type=int, default=DEFAULT_HOURS)
    parser.add_argument('--pandaid', type=int)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    since = datetime.now(dt_timezone.utc) - timedelta(hours=args.hours)
    entries = held_logs(since.replace(tzinfo=None), args.pandaid)
    summary = {'held': len(entries), 'done': 0, 'bucket': 0, 'failed': [], 'skipped': 0}
    if not entries:
        print(json.dumps(summary))
        return 0

    try:
        import boto3
        s3 = boto3.Session(profile_name=PROFILE).client('s3', region_name=REGION)
        client, uploader = rucio_clients()
    except Exception as exc:
        _log(f'log-rescue: no usable credential: {exc}')
        summary['error'] = f'credential: {exc}'
        print(json.dumps(summary))
        return 5

    state = load_state()
    for entry in entries:
        pandaid = str(entry[0])
        record = state.get(pandaid, {})
        if record.get('outcome') in ('done', 'bucket'):
            summary[record['outcome']] += 1
            continue
        if record.get('attempts', 0) >= MAX_ATTEMPTS:
            summary['skipped'] += 1
            continue
        try:
            outcome, detail = rescue(client, uploader, s3, entry, args.dry_run)
        except Exception as exc:
            outcome, detail = 'failed', f'{type(exc).__name__}: {exc}'
        _log(f'log-rescue: {pandaid} {outcome}: {detail}')
        if outcome == 'failed':
            summary['failed'].append(f'{pandaid}: {detail}'[:300])
            state[pandaid] = {'outcome': 'failed', 'attempts': record.get('attempts', 0) + 1,
                              'reason': detail[:500], 'at': int(time.time())}
        elif outcome != 'dry_run':
            summary[outcome] += 1
            state[pandaid] = {'outcome': outcome, 'detail': detail[:500], 'at': int(time.time())}
    if not args.dry_run:
        save_state(state)
    print(json.dumps(summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
