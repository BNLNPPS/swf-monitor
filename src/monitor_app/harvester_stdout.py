"""Harvester stdout records: capture, store, read.

The Kubernetes harvester queues (BNL_ePIC_GOOGLE today) upload a
worker's stdout alone, to the PanDA server's cache, and the server
purges that cache on a flat seven-day mtime cutoff (panda-server
copyArchive.py); nothing else of those jobs' logs exists. This module
copies the stdout of the jobs worth keeping while it is there, so the
job page and the dig read our copy for as long as the record needs it
(docs/EPICPROD_OPS.md, Harvester stdout records).

Which jobs: every job whose pilot id names a cache stdout, failed
always, finished behind the ``harvester_stdout.finished`` switch (their
account is the payload report). The capture runs hourly, well inside
the week, and a fetch that fails leaves a marker with its reason, so a
copy that could not be made is visible rather than absent; a 404 is
the cache having let the file go.

The store is a filesystem tree outside the reclaimable scratch cache,
so the thirty-day prune of ``panda-logs`` never touches it. Layout::

    <root>/<jeditaskid>/<pandaid>.stdout.gz   the text, gzip (220 KB against 4 MB)
    <root>/<jeditaskid>/<pandaid>.json     source, fetched, bytes, status
    <root>/<jeditaskid>/<pandaid>.error    why the fetch failed

Lifetime: ``harvester_stdout.keep_days``, 30 by default (0 keeps them);
the hourly pass prunes copies older than that by their capture time.
"""
import gzip
import json
import logging
import os
import ssl
import urllib.request
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.db import connections

from .panda.constants import PANDA_SCHEMA

logger = logging.getLogger(__name__)

# A worker's stdout is 120 to 220 KB gzip on the wire, about 4 MB as text
# (the whole pilot log); the cap is far above that and a body that
# reaches it is recorded as capped rather than silently shortened.
FETCH_MAX_BYTES = 64 * 1024 * 1024
FETCH_TIMEOUT_S = 60
DEFAULTS = {'finished': False, 'keep_days': 30, 'backfill_days': 7}


def store_root():
    return getattr(settings, 'HARVESTER_STDOUT_ROOT',
                   '/data/wenauseic/swf-monitor/harvester-stdout')


def setting(key):
    from .models import SysConfig
    return SysConfig.get_setting(f'harvester_stdout.{key}', DEFAULTS[key])


def _paths(root, jeditaskid, pandaid):
    d = os.path.join(root, str(int(jeditaskid or 0)))
    base = os.path.join(d, str(int(pandaid)))
    return d, f'{base}.stdout.gz', f'{base}.json', f'{base}.error'


def stored(pandaid, jeditaskid=None, root=None):
    """The copy of one job, or None: ``{pandaid, status, path, body,
    bytes, captured_at, source}``, status 'captured' or 'failed' (the
    failed form's body is the reason)."""
    root = root or store_root()
    if jeditaskid is None:
        jeditaskid = _task_of(pandaid)
        if jeditaskid is None:
            return None
    _, out, meta, err = _paths(root, jeditaskid, pandaid)
    info = {}
    try:
        with open(meta, encoding='utf-8') as f:
            info = json.load(f) or {}
    except (OSError, ValueError):
        info = {}
    for path, status in ((out, 'captured'), (err, 'failed')):
        if os.path.exists(path):
            try:
                if path.endswith('.gz'):
                    with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as f:
                        body = f.read()
                else:
                    with open(path, encoding='utf-8', errors='replace') as f:
                        body = f.read()
            except (OSError, EOFError) as e:                  # noqa: BLE001
                logger.error('harvester stdout unreadable at %s: %s', path, e)
                return None
            return {'pandaid': int(pandaid), 'status': status, 'path': path,
                    'body': body, 'bytes': len(body),
                    'captured_at': info.get('fetched'), 'source': info.get('source')}
    return None


def _task_of(pandaid):
    from .models import EpicProdJob
    row = EpicProdJob.objects.filter(pandaid=int(pandaid)).only('jeditaskid').first()
    if row is not None and row.jeditaskid:
        return int(row.jeditaskid)
    try:
        with connections['panda'].cursor() as cursor:
            cursor.execute(f'SELECT "jeditaskid" FROM "{PANDA_SCHEMA}"."jobsarchived4" '
                           f'WHERE "pandaid" = %s', [int(pandaid)])
            r = cursor.fetchone()
            return int(r[0]) if r and r[0] else None
    except Exception as e:                                    # noqa: BLE001
        logger.error('harvester stdout: task of %s unknown: %s', pandaid, e)
        return None


def cache_url(pilotid):
    """The PanDA-cache stdout URL a pilot id names, or ''."""
    url = str(pilotid or '').split('|')[0].strip()
    return url if ('/cache/' in url and 'pandaserver' in url) else ''


def candidates(since, finished=False, limit=None):
    """Jobs ended since ``since`` whose pilot id names a cache stdout:
    failed, and finished when asked. ``[(pandaid, jeditaskid, url,
    jobstatus, endtime)]``."""
    statuses = ['failed'] + (['finished'] if finished else [])
    sql = f"""
        SELECT "pandaid", "jeditaskid", "pilotid", "jobstatus", "endtime"
        FROM "{PANDA_SCHEMA}"."jobsarchived4"
        WHERE "processingtype" = 'epicproduction'
          AND "jobstatus" = ANY(%s)
          AND "pilotid" LIKE '%%/cache/%%'
          AND "endtime" >= %s
        ORDER BY "endtime" DESC
    """
    if limit:
        sql += f' LIMIT {int(limit)}'
    try:
        with connections['panda'].cursor() as cursor:
            cursor.execute(sql, [statuses, since])
            rows = cursor.fetchall()
    except Exception as e:                                    # noqa: BLE001
        logger.error('harvester stdout candidate query failed: %s', e)
        return []
    out = []
    for pandaid, jeditaskid, pilotid, status, endtime in rows:
        url = cache_url(pilotid)
        if url and jeditaskid:
            out.append((int(pandaid), int(jeditaskid), url, status, endtime))
    return out


def _fetch(url):
    """The stdout as gzip bytes and its text length. The server serves
    it gzip-encoded under a .out name and the bytes are kept as they
    came; the chain of its TLS certificate is not in this host's trust
    store, and no credential travels either way."""
    context = ssl._create_unverified_context()
    request = urllib.request.Request(
        url, headers={'User-Agent': 'swf-monitor/harvester-stdout',
                      'Accept-Encoding': 'gzip'})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S, context=context) as response:
        raw = response.read(FETCH_MAX_BYTES)
        encoding = (response.headers.get('Content-Encoding') or '').lower()
    if encoding == 'gzip' or raw[:2] == b'\x1f\x8b':
        text_len = len(gzip.decompress(raw))
        return raw, text_len
    return gzip.compress(raw), len(raw)


def _write(path, text):
    tmp = f'{path}.part'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def _write_bytes(path, data):
    tmp = f'{path}.part'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)


def capture(since=None, finished=None, limit=None, root=None):
    """Copy every candidate's stdout not yet stored. Idempotent: a job
    with a body or a failure marker is skipped. Returns the counts and
    the jobs captured and gone."""
    root = root or store_root()
    if finished is None:
        finished = bool(setting('finished'))
    since = since or (datetime.now(dt_timezone.utc) - timedelta(days=int(setting('backfill_days') or 7)))
    rows = candidates(since, finished=finished, limit=limit)
    counts = {'candidates': len(rows), 'skipped': 0, 'captured': 0, 'gone': 0, 'failed': 0,
              'bytes': 0}
    captured, gone = [], []
    now = datetime.now(dt_timezone.utc).isoformat()
    for pandaid, jeditaskid, url, status, endtime in rows:
        d, out, meta, err = _paths(root, jeditaskid, pandaid)
        if os.path.exists(out) or os.path.exists(err):
            counts['skipped'] += 1
            continue
        os.makedirs(d, exist_ok=True)
        try:
            body, text_len = _fetch(url)
        except Exception as e:                                # noqa: BLE001
            is_gone = getattr(e, 'code', None) == 404
            reason = (f'{"gone from the PanDA cache" if is_gone else e.__class__.__name__}: {e}\n'
                      f'source: {url}\nchecked: {now}\n')
            _write(err, reason)
            _write(meta, json.dumps({'source': url, 'fetched': now, 'status': 'gone' if is_gone else 'failed',
                                     'jobstatus': status, 'endtime': endtime.isoformat() if endtime else None}))
            counts['gone' if is_gone else 'failed'] += 1
            if is_gone:
                gone.append(pandaid)
            continue
        _write_bytes(out, body)
        _write(meta, json.dumps({'source': url, 'fetched': now, 'status': 'captured',
                                 'bytes': text_len, 'stored_bytes': len(body),
                                 'capped': len(body) >= FETCH_MAX_BYTES,
                                 'jobstatus': status,
                                 'endtime': endtime.isoformat() if endtime else None}))
        counts['captured'] += 1
        counts['bytes'] += len(body)
        captured.append(pandaid)
    return counts, captured, gone


def prune(keep_days=None, root=None, dry_run=False):
    """Remove copies older than ``keep_days`` (their fetched time); 0 or
    None keeps everything. Returns the paths removed."""
    root = root or store_root()
    keep_days = int(keep_days if keep_days is not None else (setting('keep_days') or 0))
    if keep_days <= 0:
        return []
    cutoff = datetime.now(dt_timezone.utc) - timedelta(days=keep_days)
    removed = []
    try:
        tasks = os.listdir(root)
    except OSError:
        return removed
    for task in tasks:
        tdir = os.path.join(root, task)
        if not os.path.isdir(tdir):
            continue
        for name in os.listdir(tdir):
            if not name.endswith('.json'):
                continue
            meta = os.path.join(tdir, name)
            try:
                with open(meta, encoding='utf-8') as f:
                    fetched = datetime.fromisoformat((json.load(f) or {}).get('fetched'))
            except (OSError, ValueError, TypeError):
                continue
            if fetched >= cutoff:
                continue
            base = meta[:-5]
            for path in (f'{base}.stdout.gz', f'{base}.stdout', f'{base}.error', meta):
                if os.path.exists(path):
                    removed.append(path)
                    if not dry_run:
                        try:
                            os.unlink(path)
                        except OSError as e:                  # noqa: BLE001
                            logger.error('harvester stdout prune failed for %s: %s', path, e)
    return removed
