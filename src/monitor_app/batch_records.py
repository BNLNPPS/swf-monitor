"""Batch-layer records: capture, store, read.

Every failure reason originating in the batch layer reaches PanDA cut to
a column width, and the cut falls at a character count rather than at a
clause, so what is lost is the end of the message, which is where batch
systems put the error. The condor event log the harvester keeps carries
each reason whole, is served without credentials inside the SCDF
network, and is transient: the dated directories hold about eighteen
days. This module captures those logs while they exist
(docs/ERROR_ATTRIBUTION.md, Batch-layer records).

Capture applies no filter and no classification. The body is written
verbatim to the file store and a fetch that fails leaves a marker
carrying its reason, so a failure is visible rather than an absence.
Parsing runs against the stored copy and never over the network
(monitor_app.panda.queries.condor_log_events), so a parser that is wrong
costs a re-run and leaves the record intact.

The store is a filesystem tree rather than a database table: the bodies
are write-once, read-rarely, never queried by content, and a rolling
month of them is comparable in size to the whole of swfdb, whose backup
and vacuum load they would join for nothing. Layout::

    <root>/YYYY-MM-DD/<pandaid>.log      the body, verbatim
    <root>/YYYY-MM-DD/<pandaid>.error    why the fetch failed
    <root>/keep/<pattern>/<pandaid>.log  exemplars and unmatched logs

Retention is one month, and the date directory is the unit of deletion.
Anything the learning pass rescues moves under ``keep`` before its
directory goes, so nothing at delete time has to decide anything.
"""
import logging
import os
import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.db import connections

from .panda.constants import PANDA_SCHEMA

logger = logging.getLogger(__name__)

# A condor event log measures 1 to 3 KB. The cap is far above that so it
# never trims a real log; a body that reaches it is recorded as capped
# rather than silently shortened.
FETCH_MAX_BYTES = 4 * 1024 * 1024
FETCH_TIMEOUT_S = 20
FETCH_WORKERS = 8
RETENTION_DAYS = 30


def store_root():
    """Where captured bodies live. Overridable for a test or a move."""
    return getattr(settings, 'BATCH_RECORD_ROOT',
                   '/data/wenauseic/swf-monitor/batch-logs')


def _day_dir(root, day):
    return os.path.join(root, day.isoformat())


def stored(pandaid, root=None):
    """The capture of one job, or None.

    Returns ``{pandaid, status, path, body, bytes, day}`` with status
    'captured' or 'failed'; the failed form carries the reason as its
    body. The date directory is not known from the PanDA id, so the
    month of directories is scanned, which is thirty stats.
    """
    root = root or store_root()
    try:
        days = sorted((d for d in os.listdir(root) if d[:2].isdigit()),
                      reverse=True)
    except OSError:
        return None
    for day in days:
        for suffix, status in (('.log', 'captured'), ('.error', 'failed')):
            path = os.path.join(root, day, f'{pandaid}{suffix}')
            if os.path.exists(path):
                try:
                    with open(path, encoding='utf-8', errors='replace') as f:
                        body = f.read()
                except OSError as e:                          # noqa: BLE001
                    logger.error('batch record unreadable at %s: %s', path, e)
                    return None
                return {'pandaid': pandaid, 'status': status, 'path': path,
                        'body': body, 'bytes': len(body), 'day': day}
    return None


def _fetch(url):
    """The log body, or an exception. No retry: the caller runs daily."""
    # The SCDF log hosts serve these over TLS with a chain this host's
    # trust store does not carry. The content is an operational log
    # inside the facility and no credential travels either way.
    context = ssl._create_unverified_context()
    request = urllib.request.Request(
        url, headers={'User-Agent': 'swf-monitor/batch-records'})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S,
                                context=context) as response:
        return response.read(FETCH_MAX_BYTES).decode('utf-8',
                                                     errors='replace')


def candidates(since, limit=None):
    """Jobs whose batch record is worth having: failed and never-started
    jobs in the window that the harvester holds a batch log for.

    Returns ``[(pandaid, batchlog_url, endtime)]``. A job with no
    harvester row has no batch record to capture and is not a candidate.
    """
    sql = f"""
        SELECT j."pandaid", w."batchlog", j."endtime"
        FROM (
            SELECT "pandaid", "endtime", "modificationtime"
            FROM "{PANDA_SCHEMA}"."jobsactive4"
            WHERE "jobstatus" = 'failed' AND "modificationtime" >= %s
            UNION
            SELECT "pandaid", "endtime", "modificationtime"
            FROM "{PANDA_SCHEMA}"."jobsarchived4"
            WHERE "jobstatus" = 'failed' AND "modificationtime" >= %s
        ) j
        JOIN "{PANDA_SCHEMA}"."harvester_rel_jobs_workers" r
            ON r."pandaid" = j."pandaid"
        JOIN "{PANDA_SCHEMA}"."harvester_workers" w
            ON w."workerid" = r."workerid"
           AND w."harvesterid" = r."harvesterid"
        WHERE w."batchlog" IS NOT NULL AND w."batchlog" <> ''
        ORDER BY j."modificationtime" DESC
    """
    if limit:
        sql += f' LIMIT {int(limit)}'
    try:
        with connections['panda'].cursor() as cursor:
            cursor.execute(sql, [since, since])
            return cursor.fetchall()
    except Exception as e:                                    # noqa: BLE001
        logger.error('batch record candidate query failed: %s', e)
        return []


def capture(since=None, limit=None, root=None, day=None):
    """Capture every uncaptured candidate's condor event log.

    Idempotent: a job that already has a body or a failure marker is
    skipped, so a re-run costs one stat per job. Returns counts of
    captured, failed and skipped.
    """
    root = root or store_root()
    day = day or date.today()
    since = since or (datetime.now(dt_timezone.utc) - timedelta(days=1))
    rows = candidates(since, limit=limit)
    directory = _day_dir(root, day)
    os.makedirs(directory, exist_ok=True)

    pending = [(pid, url) for pid, url, _ in rows
               if not stored(pid, root=root)]
    counts = {'candidates': len(rows), 'skipped': len(rows) - len(pending),
              'captured': 0, 'failed': 0}

    def _one(item):
        pandaid, url = item
        try:
            body = _fetch(url)
        except Exception as e:                                # noqa: BLE001
            # A failure is recorded, never left as an absence: an absent
            # file and an unreachable log are otherwise the same thing.
            reason = f'{e.__class__.__name__}: {e}\nsource: {url}\n'
            _write(os.path.join(directory, f'{pandaid}.error'), reason)
            return 'failed'
        capped = '' if len(body) < FETCH_MAX_BYTES else (
            f'\n[capture: body reached the {FETCH_MAX_BYTES} byte cap]\n')
        header = (f'# source: {url}\n'
                  f'# fetched: {datetime.now(dt_timezone.utc).isoformat()}\n'
                  f'# bytes: {len(body)}\n')
        _write(os.path.join(directory, f'{pandaid}.log'),
               header + body + capped)
        return 'captured'

    if pending:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            for outcome in pool.map(_one, pending):
                counts[outcome] += 1
    return counts


def _write(path, text):
    """Write once, atomically, so a reader never sees a partial body."""
    tmp = f'{path}.part'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def prune(days=RETENTION_DAYS, root=None, dry_run=False):
    """Remove date directories older than the retention window.

    The directory is the unit: anything the learning pass rescued has
    already moved under ``keep``, so this decides nothing about content.
    """
    root = root or store_root()
    cutoff = date.today() - timedelta(days=days)
    removed = []
    try:
        entries = os.listdir(root)
    except OSError as e:                                      # noqa: BLE001
        logger.error('batch record store unreadable at %s: %s', root, e)
        return removed
    for name in sorted(entries):
        if name == 'keep':
            continue
        try:
            when = date.fromisoformat(name)
        except ValueError:
            continue
        if when >= cutoff:
            continue
        path = os.path.join(root, name)
        if dry_run:
            removed.append(path)
            continue
        try:
            for entry in os.scandir(path):
                os.unlink(entry.path)
            os.rmdir(path)
            removed.append(path)
        except OSError as e:                                  # noqa: BLE001
            logger.error('batch record prune failed for %s: %s', path, e)
    return removed
