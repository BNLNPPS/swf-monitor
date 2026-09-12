"""Payload reports: sweep the reports of failed jobs, file them, delete them.

A production job writes its own account of itself as objects to S3 while
it runs, because PanDA keeps job metadata for finished jobs only and the
full account of a job that fails otherwise dies with the worker
(swf-epicprod docs/JOB_REPORTING.md). A successful job's objects expire
unread. A failed job's are worth something only until what they say has
been taken, and this module takes it: read, file beside the job record,
delete.

The sweep is selective by design. A storm produces thousands of objects
that say one thing, and the payload digest PanDA already carries for
every failed job gives the signature before anything is read, so a
bounded number per distinct signature is read and the rest are deleted
unread. Reading a storm one object at a time buys no understanding.

The store belongs to the gateway, so the sweep may delete only what it
reports, and it reports every pass — the empty pass and the failed pass
included, because a pass that found nothing is the liveness signal and a
pass that errored is what the gateway's growth guard needs to hear. The
record posts after the deletes land, so the gateway's index never
records as gone something still in the bucket. Only 202 means delivered.

Filing goes to ``EpicProdJob.data['payload_report']``: the job record
already exists for the production inventory and carries a JSON field, so
a report needs no table of its own.
"""
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone as dt_timezone

from django.db import connections

from .models import EpicProdJob, PersistentState
from .panda.constants import PANDA_SCHEMA

logger = logging.getLogger(__name__)

# How many jobs of one failure signature are read before the rest of that
# signature is deleted unread. Two gives a second opinion on the reason
# without turning a storm into a read loop.
READ_PER_SIGNATURE = 2
# The gateway holds the objects seven days; a pass covers its own hour
# with generous overlap, since re-reading an object costs a GET and
# missing one costs the account of a failure.
DEFAULT_HOURS = 6
SWEEPER_ENV = os.path.expanduser('~/.epic-report-sweeper.env')
INGEST_ENV = os.path.expanduser('~/.epic-sweeper-ingest.env')
BACKLOG_KEY = 'payload_report_sweep_backlog'
POST_TIMEOUT_S = 30


def _read_env_file(path):
    """The key=value lines of a credential file, or {} when it is absent.

    A missing file turns the sweep off rather than failing it: that is
    how the channel is disabled on a host that should not sweep.
    """
    values = {}
    try:
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError as e:
        logger.error(f'payload report sweep: cannot read {path}: {e}')
    return values


def sweeper_client():
    """(s3 client, bucket, prefix) from the sweeper credential, or None.

    The credential is list, get and delete under the reports prefix and
    nothing else; it is not the write credential the jobs carry.
    """
    env = _read_env_file(SWEEPER_ENV)
    bucket = env.get('REPORT_SWEEP_BUCKET')
    key_id = env.get('REPORT_SWEEP_ACCESS_KEY_ID')
    secret = env.get('REPORT_SWEEP_SECRET_ACCESS_KEY')
    if not (bucket and key_id and secret):
        logger.error(
            f'payload report sweep: {SWEEPER_ENV} carries no usable credential; '
            f'the sweep is off on this host')
        return None
    try:
        import boto3
    except ImportError as e:
        logger.error(f'payload report sweep: boto3 unavailable: {e}')
        return None
    client = boto3.client(
        's3', region_name=env.get('REPORT_SWEEP_REGION') or 'us-east-1',
        aws_access_key_id=key_id, aws_secret_access_key=secret)
    prefix = (env.get('REPORT_SWEEP_PREFIX') or 'reports/').strip('/') + '/'
    return client, bucket, prefix


def candidates(since, limit=None):
    """Failed jobs in the window, with what names their failure signature.

    Returns ``[{pandaid, site, transexitcode, piloterrorcode, digest}]``.
    The digest is the payload's own trail, which rides the job metrics
    string and survives a failed job where metadata does not.
    """
    sql = f"""
        SELECT "pandaid", "computingsite", "transexitcode", "piloterrorcode",
               "jobmetrics", "jeditaskid", "modificationhost", "processingtype"
        FROM "{PANDA_SCHEMA}"."jobsactive4"
        WHERE "jobstatus" = 'failed' AND "modificationtime" >= %s
        UNION
        SELECT "pandaid", "computingsite", "transexitcode", "piloterrorcode",
               "jobmetrics", "jeditaskid", "modificationhost", "processingtype"
        FROM "{PANDA_SCHEMA}"."jobsarchived4"
        WHERE "jobstatus" = 'failed' AND "modificationtime" >= %s
    """
    if limit:
        sql += f' LIMIT {int(limit)}'
    rows = []
    try:
        with connections['panda'].cursor() as cursor:
            cursor.execute(sql, [since, since])
            for (pandaid, site, transexit, piloterr, metrics, jeditaskid,
                 node, processingtype) in cursor.fetchall():
                rows.append({
                    'pandaid': int(pandaid),
                    'processingtype': processingtype or '',
                    'site': site or '',
                    'transexitcode': str(transexit or ''),
                    'piloterrorcode': int(piloterr or 0),
                    'digest': _digest(metrics),
                    'jeditaskid': int(jeditaskid) if jeditaskid else None,
                    # The node is what a decline is a measurement of, and it
                    # is on the job record even when nothing else survives.
                    'node': node or '',
                })
    except Exception as e:                                    # noqa: BLE001
        logger.error(f'payload report sweep: candidate query failed: {e}')
    return rows


def _digest(jobmetrics):
    """The payload trail carried in the job metrics string, or ''."""
    for token in str(jobmetrics or '').split():
        if token.startswith('payloadTrail='):
            return token.partition('=')[2]
    return ''


def signature(job):
    """What makes two failures the same for the purpose of reading one.

    Site, the payload's exit code, the pilot's error code and the trail:
    a storm repeats all four, and two failures that differ in any of them
    are worth reading separately.
    """
    return (job['site'], job['transexitcode'], job['piloterrorcode'],
            job['digest'])


def job_objects(client, bucket, prefix, pandaid):
    """The keys of one job's report objects, in the order it wrote them."""
    keys = []
    token = None
    while True:
        kwargs = {'Bucket': bucket, 'Prefix': f'{prefix}{pandaid}/'}
        if token:
            kwargs['ContinuationToken'] = token
        response = client.list_objects_v2(**kwargs)
        keys.extend(item['Key'] for item in response.get('Contents') or [])
        token = response.get('NextContinuationToken')
        if not token:
            break

    def order(key):
        stem = key.rsplit('/', 1)[-1].rsplit('.', 1)[0]
        return int(stem) if stem.isdigit() else -1

    return sorted(keys, key=order)


def read_report(client, bucket, keys):
    """The job's own account, from the last object that parses.

    The payload rewrites its report at every stage end, so the last
    object carries the whole run; an earlier one is read only when the
    last is truncated or unparseable.
    """
    for key in reversed(keys):
        try:
            body = client.get_object(Bucket=bucket, Key=key)['Body'].read()
            return json.loads(body.decode('utf-8')), key
        except Exception as e:                                # noqa: BLE001
            logger.error(f'payload report sweep: {key} unreadable: {e}')
    return None, ''


def file_report(pandaid, report, source_key, jeditaskid=None, node=''):
    """File a job's report beside its job record. Returns True when stored.

    The report lands under ``data['payload_report']`` with the object it
    came from and the node that ran the job, so the filed copy names its
    source and can be read per node — which is what a landing decline is a
    measurement of. Nothing else on the record is touched: phase and
    failure_summary belong to the inventory's own diagnosis.
    """
    try:
        job, created = EpicProdJob.objects.get_or_create(pandaid=pandaid)
        data = dict(job.data or {})
        data['payload_report'] = {
            'report': report,
            'source_key': source_key,
            'node': node or '',
            'filed_at': datetime.now(dt_timezone.utc).isoformat(),
        }
        job.data = data
        fields = ['data', 'updated_at']
        # A row the sweep creates knows only its own job; give it the task
        # so every task-keyed surface can reach it.
        if jeditaskid and not job.jeditaskid:
            job.jeditaskid = jeditaskid
            fields.append('jeditaskid')
        job.save(update_fields=fields)
        # A trial's report is the measurement its edition was waiting for:
        # the cost lands on the edition the trial proves (PCS.md, Trials).
        # Reported and never fatal to the filing.
        try:
            from pcs.services import record_trial_cost
            record_trial_cost(pandaid, report, jeditaskid or job.jeditaskid)
        except Exception as e:                                # noqa: BLE001
            logger.error(f'payload report sweep: trial cost for {pandaid}: {e}')
        return True
    except Exception as e:                                    # noqa: BLE001
        logger.error(f'payload report sweep: cannot file {pandaid}: {e}')
        return False


def delete_keys(client, bucket, keys):
    """Delete objects, returning those actually gone."""
    gone = []
    for chunk_start in range(0, len(keys), 1000):
        chunk = keys[chunk_start:chunk_start + 1000]
        try:
            response = client.delete_objects(
                Bucket=bucket,
                Delete={'Objects': [{'Key': k} for k in chunk], 'Quiet': False})
            gone.extend(item['Key'] for item in response.get('Deleted') or [])
            for err in response.get('Errors') or []:
                logger.error(f"payload report sweep: delete {err.get('Key')} "
                             f"refused: {err.get('Message')}")
        except Exception as e:                                # noqa: BLE001
            logger.error(f'payload report sweep: delete failed: {e}')
    return gone


def post_pass(record):
    """Post the pass record to the gateway. Returns (delivered, status, reason).

    Only 202 means the record reached the gateway's spool, and a 202 is
    never re-posted. A 503, a timeout or any other 5xx leaves the record
    undelivered and it is carried to the next pass. A 400 or a 413 will
    never succeed unchanged, so it is raised rather than retried.
    """
    env = _read_env_file(INGEST_ENV)
    url = env.get('SWEEP_INGEST_URL')
    token = env.get('SWEEP_INGEST_TOKEN')
    if not (url and token):
        return False, 0, f'{INGEST_ENV} carries no endpoint and token'
    body = json.dumps(record).encode('utf-8')
    request = urllib.request.Request(
        url, data=body, method='POST',
        headers={'Content-Type': 'application/json',
                 'Authorization': f'Bearer {token}'})
    try:
        with urllib.request.urlopen(request, timeout=POST_TIMEOUT_S) as response:
            status = response.getcode()
        if status == 202:
            return True, status, ''
        return False, status, f'gateway answered {status}, not 202'
    except urllib.error.HTTPError as e:
        reason = f'gateway answered {e.code}'
        if e.code in (400, 413):
            logger.error(f'payload report sweep: {reason}; the record cannot '
                         f'succeed unchanged and is not re-posted')
        elif e.code in (401, 403):
            logger.error(f'payload report sweep: {reason}; the sweeper token '
                         f'is missing or not the sweeper account')
        return False, e.code, reason
    except Exception as e:                                    # noqa: BLE001
        return False, 0, f'pass record not delivered: {e}'


def _backlog():
    """Pass records the gateway has not accepted yet."""
    try:
        return (PersistentState.get_state() or {}).get(BACKLOG_KEY) or []
    except Exception as e:                                    # noqa: BLE001
        logger.error(f'payload report sweep: backlog unreadable: {e}')
        return []


def _set_backlog(records):
    try:
        PersistentState.update_state({BACKLOG_KEY: records})
    except Exception as e:                                    # noqa: BLE001
        logger.error(f'payload report sweep: backlog not stored: {e}')


def sweep(since, limit=None, per_signature=READ_PER_SIGNATURE, dry_run=False):
    """One pass: read a bounded sample per signature, file it, delete the rest.

    Returns the pass record, which is also what was posted. A pass that
    found nothing still posts, because the empty pass is what separates a
    quiet week from a dead sweeper.
    """
    started = datetime.now(dt_timezone.utc)
    opened = sweeper_client()
    if opened is None:
        return {'outcome': 'failed', 'reason': 'no sweeper credential',
                'window': {'from': since.isoformat(),
                           'to': started.isoformat()},
                'filed': [], 'deleted_read': [], 'deleted_unread': []}
    client, bucket, prefix = opened

    jobs = candidates(since, limit=limit)
    seen, to_read, to_drop = {}, [], []
    for job in jobs:
        key = signature(job)
        seen[key] = seen.get(key, 0) + 1
        # A canary or a reproduction is a one-off whose report is the
        # product (SEGFAULT_DIAGNOSIS.md, Reproduction): read it whatever
        # its signature's count. The bound is for storms.
        if job.get('processingtype') == 'canary':
            to_read.append(job)
            continue
        (to_read if seen[key] <= per_signature else to_drop).append(job)

    filed, deleted_read, deleted_unread, errors = [], [], [], []
    for job in to_read:
        keys = job_objects(client, bucket, prefix, job['pandaid'])
        if not keys:
            continue
        report, source_key = read_report(client, bucket, keys)
        if report is None:
            errors.append(f"{job['pandaid']}: no readable report object")
            continue
        if dry_run:
            filed.append(job['pandaid'])
            continue
        if file_report(job['pandaid'], report, source_key,
                       jeditaskid=job.get('jeditaskid'), node=job.get('node')):
            filed.append(job['pandaid'])
            deleted_read.extend(delete_keys(client, bucket, keys))
        else:
            errors.append(f"{job['pandaid']}: filing failed, objects kept")

    for job in to_drop:
        keys = job_objects(client, bucket, prefix, job['pandaid'])
        if keys and not dry_run:
            deleted_unread.extend(delete_keys(client, bucket, keys))
        elif keys:
            deleted_unread.extend(keys)

    record = {
        'outcome': 'ok' if not errors else 'partial',
        'reason': '; '.join(errors[:20]),
        # 'from' and 'to', which is what the gateway's index reads
        # (swf-remote scripts/stageout_index.py). The pass record is a
        # contract between two hosts, so its shape is pinned in
        # swf-epicprod docs/JOB_REPORTING.md rather than left to a reader.
        'window': {'from': since.isoformat(), 'to': started.isoformat()},
        'signatures': len(seen),
        'candidates': len(jobs),
        'filed': filed,
        'deleted_read': deleted_read,
        'deleted_unread': deleted_unread,
        'dry_run': bool(dry_run),
    }
    if dry_run:
        return record

    pending = _backlog() + [record]
    undelivered = []
    this_pass_delivered = False
    for entry in pending:
        delivered, status, reason = post_pass(entry)
        if delivered:
            if entry is record:
                this_pass_delivered = True
            continue
        if status in (400, 413):
            logger.error(f'payload report sweep: dropping a record the gateway '
                         f'will never accept: {reason}')
            continue
        undelivered.append(entry)
        logger.error(f'payload report sweep: {reason}; carried to the next pass')
    _set_backlog(undelivered)
    record['delivered'] = this_pass_delivered
    return record
