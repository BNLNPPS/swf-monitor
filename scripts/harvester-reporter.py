#!/usr/bin/env python3
"""harvester-reporter.py — how the harvester's queues get work.

Runs on a harvester host (pandaharvester01, osgsub01) and posts one record
per run to swf-monitor (docs/HARVESTER_REPORTER.md): per queue, the
harvester's job fetches since the last run (asked, got, the outcome, the
last attempt), the workers it submitted and for which jobs, the worker
counts the submitter last read; every failed call to the PanDA server by
daemon, call and error class; the age of every harvester log; the
harvester's process count; and, from the harvester's own database and its
condor logs, its launch limits per queue, its workers now, the workers
that ended in the interval with the batch system's reason for each that
did not finish, and its service metrics.

Standard library only, written to the host's Python 3.6. Every source that
cannot be read is delivered as an error field rather than dropped. The logs
are read from the position the previous run left, so each record covers
exactly the lines appended since it; a rotated log restarts from its
beginning and says so. Log times are UTC and delivered as such.

Usage::

    harvester-reporter.py                 # collect and post
    harvester-reporter.py --print         # collect and print, post nothing
    harvester-reporter.py --report-as osgsub01-harvester   # the record's host key

Configuration, from the environment or an environment file named by
--env (mode 600): SWF_MONITOR_URL, SWF_REPORT_TOKEN.
"""
import argparse
import ast
import configparser
import glob
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta

# The record's host key; --report-as overrides it.
HOST = socket.gethostname().split('.')[0]
LOG_DIR = '/var/log/harvester'
HARVESTER_CFG = '/opt/harvester/etc/panda/panda_harvester.cfg'
QUEUE_CONFIG = '/opt/harvester/etc/panda/panda_queueconfig.json'
# Worker states the harvester leaves; every other state is a live worker.
TERMINAL = ('finished', 'failed', 'cancelled', 'missed')
# Condor logs read per run (the workers that did not finish, newest first),
# the bytes read from the end of each, and the examples kept per reason.
MAX_CONDOR_LOGS = 300
CONDOR_LOG_TAIL = 256 * 1024
EXAMPLES = 3
REASONS_KEEP = 25
DB_TIMEOUT_S = 60
FETCHER_LOG = os.path.join(LOG_DIR, 'panda-job_fetcher.log')
SUBMITTER_LOG = os.path.join(LOG_DIR, 'panda-submitter.log')
CALL_LOGS = (('communicator', os.path.join(LOG_DIR, 'panda-communicator.log')),
             ('propagator', os.path.join(LOG_DIR, 'panda-propagator.log')))
DEFAULT_STATE = os.path.expanduser('~/.swf-harvester-reporter')
POST_TIMEOUT_S = 30
BUFFER_KEEP = 48
# A run reads at most this much of one log; more is skipped and flagged.
MAX_READ_BYTES = 64 * 1024 * 1024
RECENT_PUSHES = 50
LINE_KEEP = 400

STAMP = r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ '
GOT_RE = re.compile(STAMP + r'panda\.log\.job_fetcher: \w+\s+run <queueName=([^>]+)> '
                    r'got (\d+) jobs for prodSourceLabel=(\S+) rtype=\S+ with (.*)$')
GETTING_RE = re.compile(STAMP + r'panda\.log\.job_fetcher: \w+\s+run <queueName=([^>]+)> '
                        r'getting (\d+) jobs')
SUBMITTED_RE = re.compile(STAMP + r'panda\.log\.submitter: \w+\s+run <[^>]*queue=([^ >]+)[^>]*> '
                          r'submitted a workerID=(\d+) for PandaID=(\d+)')
STATUS_RE = re.compile(STAMP + r'panda\.log\.submitter: \w+\s+run <[^>]*queue=([^ >]+)[^>]*> '
                       r'workers status: (\{.*\})')
CHUNKS_RE = re.compile(STAMP + r'panda\.log\.submitter: \w+\s+run <[^>]*queue=([^ >]+)[^>]*> '
                       r'got (\d+) job chunks')
FAILED_RE = re.compile(STAMP + r'panda\.log\.(\w+): \w+\s+(\w+).*?failed to POST with '
                       r"<class '([\w.]+)'>")
CLASS_RE = re.compile(r"<class '([\w.]+)'>:?(.*)")
URL_RE = re.compile(r'with url: (/[\w/]+)')


def now_iso():
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')


def utc(stamp):
    return stamp.replace(' ', 'T') + 'Z'


# ── Log tailing from the previous run's position ─────────────────────────

def read_since(path, positions, first_run):
    """The lines appended to ``path`` since the last run, and how the
    read went. ``positions`` is the state map, updated in place. On the
    first run nothing is counted: the position is set to the end so the
    next run covers a known interval. A new inode or a shorter file is a
    rotation: the read restarts from the file's beginning."""
    info = {'bytes': 0, 'rotated': False, 'truncated': False}
    try:
        st = os.stat(path)
    except OSError as exc:
        info['error'] = 'cannot stat: {}'.format(exc)
        return [], info
    prev = positions.get(path) or {}
    offset = prev.get('offset', 0)
    if first_run or not prev:
        positions[path] = {'inode': st.st_ino, 'offset': st.st_size}
        info['first_run'] = True
        return [], info
    if prev.get('inode') != st.st_ino or offset > st.st_size:
        info['rotated'] = True
        offset = 0
    to_read = st.st_size - offset
    if to_read > MAX_READ_BYTES:
        info['truncated'] = True
        offset = st.st_size - MAX_READ_BYTES
        to_read = MAX_READ_BYTES
    lines = []
    try:
        with open(path, 'rb') as handle:
            handle.seek(offset)
            chunk = handle.read(to_read)
        # A partial final line belongs to the next run.
        end = chunk.rfind(b'\n') + 1
        lines = chunk[:end].decode('utf-8', 'replace').splitlines()
        info['bytes'] = end
        positions[path] = {'inode': st.st_ino, 'offset': offset + end}
    except OSError as exc:
        info['error'] = 'cannot read: {}'.format(exc)
    return lines, info


# ── The functions ────────────────────────────────────────────────────────

def outcome_of(text):
    """(outcome, error_class) of a fetch line's tail: ``OK  : took 4.5 sec``,
    ``No jobs in PanDA  : took ...``, or ``failed to POST with <class ...>``."""
    text = text.strip()
    if text.startswith('failed to POST with'):
        found = CLASS_RE.search(text)
        cls = found.group(1).rsplit('.', 1)[-1] if found else 'unknown'
        return text[:LINE_KEEP], cls
    return text.split(' : took')[0].strip(), None


def fetching(lines, info):
    queues = {}
    for line in lines:
        got = GOT_RE.match(line)
        if got:
            stamp, queue, n, label, tail = got.groups()
            q = queues.setdefault(queue, {'attempts': 0, 'jobs_got': 0, 'failed': 0,
                                          'errors': {}, 'asked': 0})
            outcome, cls = outcome_of(tail)
            q['attempts'] += 1
            q['jobs_got'] += int(n)
            if cls:
                q['failed'] += 1
                q['errors'][cls] = q['errors'].get(cls, 0) + 1
            q['last'] = {'at': utc(stamp), 'label': label, 'got': int(n),
                         'asked': q.get('_asking'), 'outcome': outcome,
                         'error_class': cls}
            if int(n) > 0:
                q['last_got_jobs_at'] = utc(stamp)
            continue
        asking = GETTING_RE.match(line)
        if asking:
            q = queues.setdefault(asking.group(2), {'attempts': 0, 'jobs_got': 0, 'failed': 0,
                                                    'errors': {}, 'asked': 0})
            q['_asking'] = int(asking.group(3))
            q['asked'] += int(asking.group(3))
    for q in queues.values():
        q.pop('_asking', None)
    return {'read': info, 'queues': queues}


def submission(lines, info):
    queues, pushes = {}, []
    for line in lines:
        hit = SUBMITTED_RE.match(line)
        if hit:
            stamp, queue, worker, pandaid = hit.groups()
            q = queues.setdefault(queue, {'workers_submitted': 0})
            q['workers_submitted'] += 1
            pushes.append({'at': utc(stamp), 'queue': queue,
                           'workerid': int(worker), 'pandaid': int(pandaid)})
            continue
        hit = STATUS_RE.match(line)
        if hit:
            stamp, queue, raw = hit.groups()
            q = queues.setdefault(queue, {'workers_submitted': 0})
            try:
                q['workers_status'] = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                q['workers_status'] = {'unparsed': raw[:LINE_KEEP]}
            q['workers_status_at'] = utc(stamp)
            continue
        hit = CHUNKS_RE.match(line)
        if hit:
            stamp, queue, n = hit.groups()
            q = queues.setdefault(queue, {'workers_submitted': 0})
            q['job_chunks'] = int(n)
            q['job_chunks_at'] = utc(stamp)
    return {'read': info, 'queues': queues, 'recent_pushes': pushes[-RECENT_PUSHES:],
            'pushes_in_interval': len(pushes)}


def failed_calls(positions, first_run):
    out = {}
    for daemon, path in CALL_LOGS:
        lines, info = read_since(path, positions, first_run)
        counts, last = {}, None
        for line in lines:
            hit = FAILED_RE.match(line)
            if not hit:
                continue
            stamp, _logger, call, cls = hit.groups()
            # The PanDA endpoint names the call better than the logging method.
            endpoint = URL_RE.search(line)
            key = '{} {}'.format(endpoint.group(1) if endpoint else call, cls.rsplit('.', 1)[-1])
            counts[key] = counts.get(key, 0) + 1
            last = {'at': utc(stamp), 'line': line[:LINE_KEEP]}
        out[daemon] = {'read': info, 'failed': sum(counts.values()),
                       'by_call_and_class': counts, 'last': last}
    return out


def daemons():
    out, now = {}, time.time()
    for path in sorted(glob.glob(os.path.join(LOG_DIR, 'panda-*.log'))):
        try:
            st = os.stat(path)
        except OSError as exc:
            out[os.path.basename(path)] = {'error': str(exc)}
            continue
        out[os.path.basename(path)] = {'age_s': int(now - st.st_mtime), 'bytes': st.st_size}
    return out


def processes():
    try:
        done = subprocess.run(['ps', '-eo', 'args'], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
    except Exception as exc:                                  # noqa: BLE001
        return {'error': '{}: {}'.format(exc.__class__.__name__, exc)}
    rows = [r for r in done.stdout.splitlines() if 'pandaharvester' in r and 'harvester-reporter' not in r]
    return {'harvester': len(rows)}


# ── The harvester's database and condor logs ────────────────────────────

def db_conf():
    """The harvester's database settings from its configuration file."""
    parser = configparser.RawConfigParser(strict=False)
    if not parser.read(HARVESTER_CFG):
        raise RuntimeError('cannot read {}'.format(HARVESTER_CFG))
    section = parser['db']
    return {key: (section.get(key) or '').strip()
            for key in ('engine', 'host', 'port', 'user', 'password', 'schema')}


UNESCAPE_RE = re.compile(r'\\(.)')
UNESCAPE = {'n': '\n', 't': '\t', '0': '\0', '\\': '\\'}


def query(conf, sql):
    """Rows of ``sql`` from the harvester's MariaDB through the mysql client
    (standard library only), NULL as None. Raises with the client's error."""
    env = dict(os.environ, MYSQL_PWD=conf['password'])
    cmd = ['mysql', '--batch', '--skip-column-names', '-u', conf['user'],
           '-h', conf['host'] or 'localhost', '-P', conf['port'] or '3306',
           conf['schema'], '-e', sql]
    done = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=DB_TIMEOUT_S, env=env)
    if done.returncode != 0:
        raise RuntimeError((done.stderr or 'mysql exit {}'.format(done.returncode)).strip()[:LINE_KEEP])
    rows = []
    for line in done.stdout.splitlines():
        rows.append([None if cell == 'NULL' else
                     UNESCAPE_RE.sub(lambda m: UNESCAPE.get(m.group(1), m.group(1)), cell)
                     for cell in line.split('\t')])
    return rows


def log_roots():
    """(logBaseURL, logDir) pairs from the queue configuration: a worker's
    log URL under a base URL is the file under that directory here."""
    try:
        with open(QUEUE_CONFIG) as handle:
            config = json.load(handle)
    except (OSError, ValueError):
        return []
    pairs = set()

    def walk(node):
        if isinstance(node, dict):
            if node.get('logBaseURL') and node.get('logDir'):
                pairs.add((node['logBaseURL'].rstrip('/'), node['logDir'].rstrip('/')))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(config)
    return sorted(pairs, key=lambda p: -len(p[0]))


def local_log(url, roots):
    if not url:
        return None
    for base, directory in roots:
        if url.startswith(base):
            return directory + '/' + url[len(base):].lstrip('/')
    return None


EVENT_RE = re.compile(r'^(\d{3}) \(([\d.]+)\) (\S+ \S+) (.*)$')
# The events that end a worker or explain why it did not run on.
REASON_EVENTS = {'005', '007', '009', '012', '021', '022', '024'}


def condor_reason(path):
    """The batch system's own account of how the worker ended: the last
    terminal or hold event of its condor event log, with its detail lines
    (``Job was aborted. removed by SYSTEM_PERIODIC_REMOVE due to ...``)."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as handle:
            handle.seek(max(0, size - CONDOR_LOG_TAIL))
            text = handle.read().decode('utf-8', 'replace')
    except OSError as exc:
        return None, 'cannot read: {}'.format(exc)
    last, current = None, None
    for line in text.splitlines():
        hit = EVENT_RE.match(line)
        if hit:
            current = {'code': hit.group(1), 'at': hit.group(3), 'text': [hit.group(4).strip()]}
            if current['code'] in REASON_EVENTS:
                last = current
            continue
        if line.startswith('...'):
            current = None
        elif current is not None and line.strip():
            current['text'].append(line.strip())
    if last is None:
        return None, None
    reason = ' '.join(last['text'])
    return {'code': last['code'], 'at': last['at'],
            'reason': re.sub(r'\s+', ' ', reason)[:LINE_KEEP]}, None


def median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0


def harvester_db(since, until):
    """The harvester's launch limits per queue, its workers now, the workers
    that ended in (since, until] with the batch system's reason for each
    that did not finish, and its latest service metrics. Each part that
    cannot be read is an error field."""
    out = {'window': {'since': since, 'until': until}}
    try:
        conf = db_conf()
    except Exception as exc:                                  # noqa: BLE001
        return dict(out, error='{}: {}'.format(exc.__class__.__name__, exc))

    try:
        cols = ('queue', 'site', 'job_type', 'resource_type', 'n_queue_limit_worker',
                'max_workers', 'n_new_workers', 'n_queue_limit_job', 'job_fetch_time', 'submit_time')
        rows = query(conf, 'SELECT queueName, siteName, jobType, resourceType, nQueueLimitWorker, '
                           'maxWorkers, nNewWorkers, nQueueLimitJob, jobFetchTime, submitTime FROM pq_table')
        out['launch_limits'] = [dict(zip(cols, r)) for r in rows]
    except Exception as exc:                                  # noqa: BLE001
        out['launch_limits'] = {'error': '{}: {}'.format(exc.__class__.__name__, exc)}

    try:
        rows = query(conf, "SELECT computingSite, status, COUNT(*) FROM work_table "
                           "WHERE status NOT IN ('{}') GROUP BY 1, 2".format("','".join(TERMINAL)))
        now = {}
        for site, status, n in rows:
            now.setdefault(site or '', {})[status] = int(n)
        out['workers_now'] = now
    except Exception as exc:                                  # noqa: BLE001
        out['workers_now'] = {'error': '{}: {}'.format(exc.__class__.__name__, exc)}

    try:
        out['workers_ended'] = workers_ended(conf, since, until)
    except Exception as exc:                                  # noqa: BLE001
        out['workers_ended'] = {'error': '{}: {}'.format(exc.__class__.__name__, exc)}

    try:
        rows = query(conf, 'SELECT hostName, creationTime, LEFT(metrics, 2000) FROM sm_table '
                           'ORDER BY creationTime DESC LIMIT 5')
        metrics = {}
        for host, created, raw in rows:
            if host in metrics:
                continue
            try:
                value = json.loads(raw) if raw else None
            except ValueError:
                value = raw
            metrics[host or ''] = {'at': created, 'metrics': value}
        out['service_metrics'] = metrics
    except Exception as exc:                                  # noqa: BLE001
        out['service_metrics'] = {'error': '{}: {}'.format(exc.__class__.__name__, exc)}
    return out


def workers_ended(conf, since, until):
    """Per site: the workers that ended in the window by status, their
    median run, and every worker that did not finish (or finished with a
    nonzero batch exit) grouped by the batch system's reason, read from
    its condor log, else the harvester's diagnostic."""
    rows = query(conf, (
        "SELECT workerID, batchID, computingSite, status, nativeStatus, nativeExitCode, "
        "TIMESTAMPDIFF(SECOND, startTime, endTime), endTime, LEFT(diagMessage, 300), "
        "JSON_UNQUOTE(JSON_EXTRACT(workAttributes, '$.batchLog')), nodeID, computingElement "
        "FROM work_table WHERE endTime > '{}' AND endTime <= '{}' ORDER BY endTime DESC"
    ).format(since, until))
    roots = log_roots()
    sites, logs_read, log_errors = {}, 0, 0
    for (worker, batch, site, status, native, exit_code, run_s, ended, diag,
         batch_log, node, ce) in rows:
        s = sites.setdefault(site or '', {'ended': 0, 'by_status': {}, '_runs': [], 'reasons': {}})
        s['ended'] += 1
        s['by_status'][status] = s['by_status'].get(status, 0) + 1
        # A worker removed before it ran has no run; the median is of finished workers.
        if status == 'finished' and run_s is not None and int(run_s) >= 0:
            s['_runs'].append(int(run_s))
        if status == 'finished' and exit_code in (None, '', '0'):
            continue
        reason = None
        path = local_log(batch_log, roots)
        if path and logs_read < MAX_CONDOR_LOGS:
            logs_read += 1
            found, error = condor_reason(path)
            if error:
                log_errors += 1
            reason = found
        text = (reason or {}).get('reason') or (diag or '').strip() or (native or status)
        key = '{} | {}'.format(status, text)
        entry = s['reasons'].setdefault(key, {
            'status': status, 'native_status': native,
            'source': 'condor log' if reason else 'harvester diagnostic',
            'event': (reason or {}).get('code'), 'reason': text, 'count': 0, 'examples': []})
        entry['count'] += 1
        if len(entry['examples']) < EXAMPLES:
            entry['examples'].append({'workerid': int(worker), 'batchid': batch, 'ended': ended,
                                      'node': node, 'ce': ce, 'batch_log': batch_log})
    for s in sites.values():
        runs = s.pop('_runs')
        s['median_finished_run_s'] = median(runs)
        reasons = sorted(s['reasons'].values(), key=lambda e: -e['count'])
        s['reasons'] = reasons[:REASONS_KEEP]
        s['reasons_dropped'] = max(0, len(reasons) - REASONS_KEEP)
    return {'sites': sites, 'workers': len(rows), 'condor_logs_read': logs_read,
            'condor_log_errors': log_errors}


def collect(state):
    """The record this run delivers; ``state`` is advanced in place."""
    started = time.time()
    record = {'host': HOST, 'collected_at': now_iso(), 'reporter_version': '1.1'}
    positions = state.setdefault('positions', {})
    last_run = state.get('last_run_at')
    first_run = not last_run
    record['interval'] = {'since': last_run, 'seconds': None, 'first_run': first_run}
    if last_run:
        try:
            since = datetime.strptime(last_run, '%Y-%m-%dT%H:%M:%SZ')
            record['interval']['seconds'] = int((datetime.utcnow() - since).total_seconds())
        except ValueError:
            record['interval']['error'] = 'unparsable last_run_at'

    lines, info = read_since(FETCHER_LOG, positions, first_run)
    record['fetching'] = fetching(lines, info)
    lines, info = read_since(SUBMITTER_LOG, positions, first_run)
    record['submission'] = submission(lines, info)
    record['failed_calls'] = failed_calls(positions, first_run)
    record['daemons'] = daemons()
    record['processes'] = processes()
    # The database window: since the previous run, or the last five
    # minutes on a first run. Harvester times are UTC.
    until = datetime.utcnow()
    since = until - timedelta(minutes=5)
    if last_run:
        try:
            since = datetime.strptime(last_run, '%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            pass
    record['harvester_db'] = harvester_db(since.strftime('%Y-%m-%d %H:%M:%S'),
                                          until.strftime('%Y-%m-%d %H:%M:%S'))
    record['collect_seconds'] = round(time.time() - started, 2)
    state['last_run_at'] = record['collected_at']
    return record


# ── Delivery (the house pattern: env file, post, buffer) ─────────────────

def load_state(state_dir):
    path = os.path.join(state_dir, 'state.json')
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_state(state_dir, state):
    try:
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, 'state.json')
        with open(path + '.tmp', 'w') as handle:
            json.dump(state, handle)
        os.replace(path + '.tmp', path)
    except OSError as exc:
        print('could not save state: {}'.format(exc), file=sys.stderr)


def load_env(path):
    """Read KEY=value lines from an environment file, if there is one."""
    if not path or not os.path.exists(path):
        return
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def post(record, url, token):
    """Deliver one record. Returns None on success, the reason on failure."""
    import urllib.request
    body = json.dumps(record).encode('utf-8')
    endpoint = '{}/api/host-reports/{}/'.format(url.rstrip('/'), HOST)
    request = urllib.request.Request(
        endpoint, data=body, method='POST',
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Token {}'.format(token)})
    # The monitor is inside the facility and serves a certificate this
    # host's trust store may not carry; the token is the authentication.
    context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=POST_TIMEOUT_S,
                                    context=context) as response:
            response.read()
        return None
    except Exception as exc:                                  # noqa: BLE001
        return '{}: {}'.format(exc.__class__.__name__, exc)


def buffered(state_dir):
    """Records held from earlier runs the monitor could not take."""
    try:
        names = sorted(os.listdir(state_dir))
    except OSError:
        return []
    return [os.path.join(state_dir, n) for n in names
            if n.startswith('record-') and n.endswith('.json')]


def buffer_record(state_dir, record):
    """Hold a record for the next run, keeping the buffer bounded."""
    try:
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, 'record-{}.json'.format(
            record['collected_at'].replace(':', '')))
        with open(path, 'w') as handle:
            json.dump(record, handle)
        held = buffered(state_dir)
        for stale in held[:-BUFFER_KEEP]:
            os.unlink(stale)
    except OSError as exc:
        print('could not buffer the record: {}'.format(exc), file=sys.stderr)


def main():
    global HOST
    parser = argparse.ArgumentParser()
    parser.add_argument('--print', dest='show', action='store_true',
                        help='print the record, post nothing, leave the state untouched')
    parser.add_argument('--env', default=os.path.expanduser('~/.swf-harvester-reporter.env'))
    parser.add_argument('--report-as', default=HOST,
                        help='the host key the record is stored under (default: this host)')
    parser.add_argument('--state', default=None,
                        help='state directory (default: one per host key; the home is shared across hosts)')
    args = parser.parse_args()
    HOST = args.report_as
    if args.state is None:
        args.state = DEFAULT_STATE if HOST == 'pandaharvester01' else '{}-{}'.format(DEFAULT_STATE, HOST)

    state = load_state(args.state)
    record = collect(state)
    if args.show:
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0
    save_state(args.state, state)

    load_env(args.env)
    url = os.environ.get('SWF_MONITOR_URL')
    token = os.environ.get('SWF_REPORT_TOKEN')
    if not url or not token:
        print('SWF_MONITOR_URL and SWF_REPORT_TOKEN are required '
              '(environment or --env file)', file=sys.stderr)
        buffer_record(args.state, record)
        return 2

    # The backlog first, oldest first, so a reader sees the sequence.
    for path in buffered(args.state):
        try:
            with open(path) as handle:
                held = json.load(handle)
        except (OSError, ValueError):
            os.unlink(path)
            continue
        if post(held, url, token) is None:
            os.unlink(path)
        else:
            break

    failure = post(record, url, token)
    if failure:
        print('post failed, record buffered: {}'.format(failure), file=sys.stderr)
        buffer_record(args.state, record)
        return 1
    fetch = (record.get('fetching') or {}).get('queues') or {}
    print('reported: {} queue(s), {} fetch attempt(s), {} failed, {} worker(s) submitted'.format(
        len(fetch), sum(q['attempts'] for q in fetch.values()),
        sum(q['failed'] for q in fetch.values()),
        (record.get('submission') or {}).get('pushes_in_interval')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
