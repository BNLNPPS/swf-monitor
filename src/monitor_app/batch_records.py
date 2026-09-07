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
import hashlib
import json
import logging
import os
import re
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


def _keep_paths(root, pandaid):
    """Where a rescued copy of this job's log would be, under ``keep``."""
    keep_root = os.path.join(root, 'keep')
    try:
        buckets = os.listdir(keep_root)
    except OSError:
        return []
    return [os.path.join(keep_root, bucket, f'{pandaid}.log')
            for bucket in sorted(buckets)]


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
    # A rescued exemplar outlives its date directory, and a link to it must
    # outlive it too: the knowledge base points at these jobs by name.
    for path in _keep_paths(root, pandaid):
        if os.path.exists(path):
            try:
                with open(path, encoding='utf-8', errors='replace') as f:
                    body = f.read()
            except OSError as e:                              # noqa: BLE001
                logger.error('rescued batch record unreadable at %s: %s', path, e)
                return None
            return {'pandaid': pandaid, 'status': 'captured', 'path': path,
                    'body': body, 'bytes': len(body), 'day': 'kept'}
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


# ── the learning pass ────────────────────────────────────────────────────
# The corpus is the authority on what a condor event log contains. A pass
# over it before it ages out yields the catalogue of codes that actually
# occur, the taxonomy of reasons that recur, and the separation of
# boilerplate from signal — and rescues one raw log per pattern, plus every
# log matching no pattern, so the knowledge base always keeps the evidence
# behind it (docs/ERROR_ATTRIBUTION.md, Retention and learning).

KNOWLEDGE_FILE = 'knowledge.json'
# A line present in most logs is the shape of the format, not the account of
# a failure. Two thirds is high enough that a common failure mode does not
# qualify and low enough to catch the ceremony.
BOILERPLATE_SHARE = 0.66
_NUM_RE = re.compile(r'\d+')
_HEX_RE = re.compile(r'\b[0-9a-f]{8,}\b', re.I)
_PATH_RE = re.compile(r'/[\w./@+-]{4,}')
_HOST_RE = re.compile(r'\b[\w-]+(?:\.[\w-]+){2,}\b')


def normalize(text):
    """The shape of a line, with what varies between jobs removed.

    Two failures are the same shape when their normalized text matches:
    hosts, paths, hexadecimal ids and numbers carry the instance, not the
    kind. Derived from the corpus rather than declared, so a pattern is
    whatever the logs repeat.
    """
    out = _HOST_RE.sub('<host>', text or '')
    out = _PATH_RE.sub('<path>', out)
    out = _HEX_RE.sub('<hex>', out)
    out = _NUM_RE.sub('#', out)
    return ' '.join(out.split())[:400]


def _iter_logs(root):
    """(pandaid, day, path, body) for every captured body in the store."""
    for name in sorted(os.listdir(root)):
        if name == 'keep':
            continue
        day_dir = os.path.join(root, name)
        if not os.path.isdir(day_dir):
            continue
        for entry in sorted(os.listdir(day_dir)):
            if not entry.endswith('.log'):
                continue
            path = os.path.join(day_dir, entry)
            try:
                with open(path, errors='replace') as handle:
                    body = handle.read()
            except OSError as e:                              # noqa: BLE001
                logger.error('batch record %s unreadable: %s', path, e)
                continue
            yield entry[:-4], name, path, body


def learn(root=None, rescue=True):
    """Mine the corpus and write the knowledge base. Returns its summary.

    Nothing here classifies a log at capture time; the capture stays
    verbatim and this is the only reader that generalizes over it, so a
    better pass re-runs against the same bodies.
    """
    from .panda.queries import CONDOR_EVENT_NAMES, parse_condor_log
    root = root or store_root()
    codes, patterns, line_docs = {}, {}, {}
    logs = 0
    for pandaid, day, path, body in _iter_logs(root):
        logs += 1
        seen_lines = set()
        for line in body.splitlines():
            code = line[:3]
            if code.isdigit() and line[3:4] == ' ' and '(' in line:
                entry = codes.setdefault(code, {
                    'code': code,
                    'name': CONDOR_EVENT_NAMES.get(code, ''),
                    'named': code in CONDOR_EVENT_NAMES,
                    'count': 0, 'logs': 0, 'first_seen': day, 'last_seen': day,
                    'example': line.strip()[:300]})
                entry['count'] += 1
                entry['last_seen'] = max(entry['last_seen'], day)
                entry['first_seen'] = min(entry['first_seen'], day)
                if code not in seen_lines:
                    entry['logs'] += 1
                    seen_lines.add(code)
            shape = normalize(line.strip())
            if shape:
                line_docs.setdefault(shape, set()).add(pandaid)
        events = parse_condor_log(body, source=path)
        for event in events.get('events') or []:
            shape = normalize(event['text'])
            if not shape:
                continue
            pattern = patterns.setdefault(shape, {
                'shape': shape, 'code': event['code'],
                'event': event.get('event') or '',
                'count': 0, 'jobs': [], 'first_seen': day, 'last_seen': day,
                'example': event['text'][:600], 'exemplar': pandaid})
            pattern['count'] += 1
            pattern['last_seen'] = max(pattern['last_seen'], day)
            pattern['first_seen'] = min(pattern['first_seen'], day)
            if len(pattern['jobs']) < 20 and pandaid not in pattern['jobs']:
                pattern['jobs'].append(pandaid)

    boilerplate = sorted(
        shape for shape, docs in line_docs.items()
        if logs and len(docs) / logs >= BOILERPLATE_SHARE)
    knowledge = {
        'built_at': datetime.now(dt_timezone.utc).isoformat(),
        'logs': logs,
        'codes': sorted(codes.values(), key=lambda c: -c['count']),
        'unnamed_codes': sorted(c['code'] for c in codes.values()
                                if not c['named']),
        'patterns': sorted(patterns.values(), key=lambda p: -p['count']),
        'boilerplate': boilerplate,
    }
    if rescue:
        knowledge['rescued'] = _rescue(root, knowledge)
    _write(os.path.join(root, KNOWLEDGE_FILE),
           json.dumps(knowledge, indent=2, sort_keys=True))
    return knowledge


def knowledge(root=None):
    """The knowledge base as last built, or None."""
    path = os.path.join(root or store_root(), KNOWLEDGE_FILE)
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _rescue(root, knowledge_base):
    """Keep one raw log per pattern, and every log matching no pattern.

    Retention deletes a date directory whole, so anything that must outlive
    it moves under ``keep`` first. One exemplar per pattern bounds the
    permanent set against a storm; an unmatched log is kept entire, because
    a novel shape is the thing the corpus cannot reconstruct later.
    """
    keep_root = os.path.join(root, 'keep')
    shapes = {p['shape'] for p in knowledge_base['patterns']}
    exemplars = {p['exemplar']: p['shape'] for p in knowledge_base['patterns']}
    rescued = {'exemplars': 0, 'unmatched': 0}
    for pandaid, day, path, body in _iter_logs(root):
        wanted, bucket = None, None
        if pandaid in exemplars:
            wanted, bucket = 'exemplars', _shape_dir(exemplars[pandaid])
        else:
            from .panda.queries import parse_condor_log
            events = parse_condor_log(body, source=path)
            found = {normalize(e['text']) for e in (events.get('events') or [])}
            if not (found & shapes):
                wanted, bucket = 'unmatched', 'unmatched'
        if not wanted:
            continue
        target_dir = os.path.join(keep_root, bucket)
        target = os.path.join(target_dir, f'{pandaid}.log')
        if os.path.exists(target):
            continue
        try:
            os.makedirs(target_dir, exist_ok=True)
            with open(target, 'w') as handle:
                handle.write(body)
            rescued[wanted] += 1
        except OSError as e:                                  # noqa: BLE001
            logger.error('batch record %s not rescued: %s', pandaid, e)
    return rescued


def _shape_dir(shape):
    """A stable directory name for a pattern, from its shape."""
    return hashlib.sha1(shape.encode('utf-8')).hexdigest()[:12]
