#!/usr/bin/env python3
"""harvester-reporter.py — how the harvester's queues get work.

Runs on pandaharvester01 and posts one record per run to swf-monitor
(docs/HARVESTER_REPORTER.md): per queue, the harvester's job fetches since
the last run (asked, got, the outcome, the last attempt), the workers it
submitted and for which jobs, the worker counts the submitter last read;
every failed call to the PanDA server by daemon, call and error class; the
age of every harvester log; and the harvester's process count.

Standard library only, written to the host's Python 3.6. Every source that
cannot be read is delivered as an error field rather than dropped. The logs
are read from the position the previous run left, so each record covers
exactly the lines appended since it; a rotated log restarts from its
beginning and says so. Log times are UTC and delivered as such.

Usage::

    harvester-reporter.py                 # collect and post
    harvester-reporter.py --print         # collect and print, post nothing

Configuration, from the environment or an environment file named by
--env (mode 600): SWF_MONITOR_URL, SWF_REPORT_TOKEN.
"""
import argparse
import ast
import glob
import json
import os
import re
import ssl
import subprocess
import sys
import time
from datetime import datetime

HOST = 'pandaharvester01'
LOG_DIR = '/var/log/harvester'
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


def collect(state):
    """The record this run delivers; ``state`` is advanced in place."""
    started = time.time()
    record = {'host': HOST, 'collected_at': now_iso(), 'reporter_version': '1.0'}
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
    parser = argparse.ArgumentParser()
    parser.add_argument('--print', dest='show', action='store_true',
                        help='print the record, post nothing, leave the state untouched')
    parser.add_argument('--env', default=os.path.expanduser('~/.swf-harvester-reporter.env'))
    parser.add_argument('--state', default=DEFAULT_STATE)
    args = parser.parse_args()

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
