#!/usr/bin/env python3
"""panda-server-reporter.py — what only the PanDA server host can see.

Runs on pandaserver01 and posts one record per run to swf-monitor
(docs/PANDA_SERVER_REPORTER.md): the web tier's request and status
counts since the last run, the error log's markers, the age of every
PanDA daemon log, the four PanDA units with their restart counters, the
httpd and pandaserver processes with their resident memory, the
database's reachability from this host, and the host's own load,
memory and volumes. The platform component merges the record as its
server_host group (docs/SNAPPER_PLATFORM.md).

Standard library only, no virtual environment. Every source that
cannot be read is delivered as an error field rather than dropped,
because a missing field and an unreadable source are otherwise the
same thing to a reader. The access and error logs are read from the
position the previous run left, so each record covers exactly the
lines appended since it; a rotated log restarts from its beginning and
says so.

Usage::

    panda-server-reporter.py                 # collect and post
    panda-server-reporter.py --print         # collect and print, post nothing

Configuration, from the environment or an environment file named by
--env (mode 600): SWF_MONITOR_URL, SWF_REPORT_TOKEN.
"""
import argparse
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime

HOST = 'pandaserver01'
LOG_DIR = '/var/log/panda'
ACCESS_LOG = os.path.join(LOG_DIR, 'panda_server_access_log')
ERROR_LOG = os.path.join(LOG_DIR, 'panda_server_error_log')
HTTPD_CONF = '/opt/panda/etc/panda/panda_server-httpd.conf'
SERVER_CFG = '/etc/panda/panda_server.cfg'
UNITS = ('panda_httpd', 'panda_daemon', 'panda_jedi', 'panda_mcp')
DEFAULT_STATE = os.path.expanduser('~/.swf-panda-server-reporter')
POST_TIMEOUT_S = 30
BUFFER_KEEP = 48
# A run reads at most this much appended log; beyond it the record
# says it was truncated rather than stalling the reporter on a flood.
MAX_READ_BYTES = 64 * 1024 * 1024
TOP_PATHS = 20

# Apache combined-style access line as the PanDA web tier writes it:
# [date] client "METHOD path HTTP/x" status bytes
ACCESS_RE = re.compile(
    r'^\[([^\]]+)\] (\S+) "(\S+) (\S+)[^"]*" (\d{3}) (\S+)')
# Apache error line level: [module:level]
ERROR_LEVEL_RE = re.compile(r'\] \[(\w+):(\w+)\]')
# Error-log markers worth counting by name; the level counts carry the
# rest. Each is a substring of the line as Apache or mod_wsgi writes it.
ERROR_MARKERS = (
    ('worker_saturation', 'MaxRequestWorkers'),
    ('wsgi_timeout', 'Timeout when reading response headers'),
    ('wsgi_truncated', 'Truncated or oversized response headers'),
    ('ssl_read', 'SSL_read'),
    ('child_exit_signal', 'exit signal'),
    ('no_memory', 'Cannot allocate memory'),
)


def now_iso():
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')


def run(cmd, timeout=30):
    """A command's stdout, or None with the reason on the record."""
    try:
        done = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=timeout)
    except Exception as exc:                                  # noqa: BLE001
        return None, '{}: {}'.format(exc.__class__.__name__, exc)
    if done.returncode != 0:
        return None, 'exit {}: {}'.format(done.returncode,
                                          (done.stderr or '').strip()[:400])
    return done.stdout, None


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


def endpoint_class(path):
    """The request's class for counting: the two pilot calls that carry
    the workload, the rest of the pilot API, harvester, the schedconfig
    cache, statistics, other."""
    if path.endswith('/update_job') or path.endswith('/updateJob'):
        return 'update_job'
    if path.endswith('/acquire_jobs') or path.endswith('/getJob'):
        return 'acquire_jobs'
    if '/pilot/' in path:
        return 'pilot_other'
    if 'harvester' in path.lower():
        return 'harvester'
    if path.startswith('/cache/'):
        return 'cache'
    if '/statistics/' in path:
        return 'statistics'
    return 'other'


def web_tier(lines, info):
    """Requests since the last run by endpoint class and status class,
    the top paths, and the 5xx count the platform view plots."""
    out = {'read': info, 'requests': 0, 'unparsed': 0,
           'by_endpoint': {}, 'by_status_class': {}, 'status_5xx': 0,
           'paths': {}, 'clients': 0}
    paths = {}
    clients = set()
    for line in lines:
        found = ACCESS_RE.match(line)
        if not found:
            out['unparsed'] += 1
            continue
        client, path, status = found.group(2), found.group(4), found.group(5)
        path = path.split('?', 1)[0]
        out['requests'] += 1
        cls = endpoint_class(path)
        out['by_endpoint'][cls] = out['by_endpoint'].get(cls, 0) + 1
        sc = status[0] + 'xx'
        out['by_status_class'][sc] = out['by_status_class'].get(sc, 0) + 1
        if status.startswith('5'):
            out['status_5xx'] += 1
        paths[path] = paths.get(path, 0) + 1
        clients.add(client)
    top = sorted(paths.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_PATHS]
    out['paths'] = dict(top)
    out['paths_distinct'] = len(paths)
    out['clients'] = len(clients)
    return out


def error_markers(lines, info):
    """Error-log lines since the last run by level and by named marker,
    with the last few non-info lines for the card."""
    out = {'read': info, 'lines': len(lines), 'by_level': {},
           'markers': {name: 0 for name, _ in ERROR_MARKERS},
           'recent': []}
    for line in lines:
        found = ERROR_LEVEL_RE.search(line)
        level = found.group(2) if found else 'unknown'
        out['by_level'][level] = out['by_level'].get(level, 0) + 1
        for name, needle in ERROR_MARKERS:
            if needle in line:
                out['markers'][name] += 1
        if level not in ('info', 'debug', 'trace1', 'trace2', 'trace3',
                         'trace4', 'trace5', 'trace6', 'trace7', 'trace8'):
            out['recent'].append(line[:300])
    out['recent'] = out['recent'][-5:]
    return out


def web_limits():
    """The web tier's declared capacity, from the httpd configuration."""
    out = {}
    try:
        with open(HTTPD_CONF) as handle:
            text = handle.read()
    except OSError as exc:
        return {'error': 'cannot read {}: {}'.format(HTTPD_CONF, exc)}
    for key, pattern in (
            ('max_request_workers', r'^\s*MaxRequestWorkers\s+(\d+)'),
            ('server_limit', r'^\s*ServerLimit\s+(\d+)'),
            ('threads_per_child', r'^\s*ThreadsPerChild\s+(\d+)'),
            ('wsgi_daemon_processes',
             r'WSGIDaemonProcess\s+\S+\s+processes=(\d+)')):
        found = re.findall(pattern, text, re.M)
        out[key] = int(found[-1]) if found else None
    return out


# ── Daemons, units, processes ────────────────────────────────────────────

def daemons():
    """Every PanDA log on the host by the age of its last write: a
    daemon is named by its log, and that age is what says whether it is
    working (the submit host reporter's idiom)."""
    out = {}
    try:
        names = sorted(n for n in os.listdir(LOG_DIR) if n.endswith('.log'))
    except OSError as exc:
        return {'error': 'cannot list {}: {}'.format(LOG_DIR, exc)}
    now = time.time()
    for name in names:
        label = name[len('panda-'):-len('.log')] if name.startswith('panda-') \
            else name[:-len('.log')]
        path = os.path.join(LOG_DIR, name)
        try:
            out[label] = {'log_age_seconds': int(now - os.path.getmtime(path)),
                          'log_bytes': os.path.getsize(path)}
        except OSError as exc:
            out[label] = {'error': str(exc)}
    return out


def services():
    """The four PanDA units: state, restarts since boot, seconds active."""
    out = {}
    try:
        with open('/proc/uptime') as handle:
            uptime = float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        uptime = None
    text, err = run(['systemctl', 'show', '-p', 'Id', '-p', 'ActiveState',
                     '-p', 'SubState', '-p', 'NRestarts',
                     '-p', 'ActiveEnterTimestampMonotonic', '--no-pager']
                    + list(UNITS))
    if err:
        return {'error': 'systemctl unavailable: {}'.format(err)}
    for block in (text or '').strip().split('\n\n'):
        props = dict(line.split('=', 1) for line in block.splitlines()
                     if '=' in line)
        unit = (props.get('Id') or '').replace('.service', '')
        if not unit:
            continue
        entry = {'active': props.get('ActiveState'),
                 'sub': props.get('SubState'),
                 'restarts': int(props.get('NRestarts') or 0)}
        mono = props.get('ActiveEnterTimestampMonotonic')
        if uptime is not None and mono and mono.isdigit() and int(mono):
            entry['active_seconds'] = int(uptime - int(mono) / 1e6)
        out[unit] = entry
    for unit in UNITS:
        out.setdefault(unit, {'error': 'not reported by systemctl'})
    return out


def processes():
    """The httpd workers and the pandaserver python processes: count and
    resident memory, read from /proc."""
    out = {'httpd': {'count': 0, 'rss_mb': 0.0},
           'pandaserver': {'count': 0, 'rss_mb': 0.0}}
    try:
        pids = [p for p in os.listdir('/proc') if p.isdigit()]
    except OSError as exc:
        return {'error': 'cannot list /proc: {}'.format(exc)}
    for pid in pids:
        try:
            with open('/proc/{}/cmdline'.format(pid), 'rb') as handle:
                cmd = handle.read().replace(b'\0', b' ').decode('utf-8',
                                                                'replace')
            if 'panda_server-httpd.conf' in cmd:
                bucket = 'httpd'
            elif 'pandaserver' in cmd and 'python' in cmd:
                bucket = 'pandaserver'
            else:
                continue
            rss_kb = 0
            with open('/proc/{}/status'.format(pid)) as handle:
                for line in handle:
                    if line.startswith('VmRSS:'):
                        rss_kb = int(line.split()[1])
                        break
        except (OSError, ValueError, IndexError):
            continue                    # the process ended under the read
        out[bucket]['count'] += 1
        out[bucket]['rss_mb'] += rss_kb / 1024.0
    for bucket in out.values():
        bucket['rss_mb'] = round(bucket['rss_mb'], 1)
    return out


# ── Database reachability, host ──────────────────────────────────────────

def database():
    """A timed TCP connect to the database host the server configuration
    names. No credential is used, so this is reachability and the
    network's cost, not a query."""
    out = {}
    host = port = None
    try:
        with open(SERVER_CFG) as handle:
            for line in handle:
                key, _, value = line.partition('=')
                key = key.strip()
                if key == 'dbhost':
                    host = value.strip()
                elif key == 'dbport':
                    port = int(value.strip())
    except (OSError, ValueError) as exc:
        return {'error': 'cannot read {}: {}'.format(SERVER_CFG, exc)}
    if not host or not port:
        return {'error': 'dbhost/dbport not found in {}'.format(SERVER_CFG)}
    out['host'] = host
    out['port'] = port
    started = time.time()
    try:
        sock = socket.create_connection((host, port), timeout=5)
        sock.close()
        out['connect_ms'] = round((time.time() - started) * 1000, 1)
        out['reachable'] = True
    except OSError as exc:
        out['reachable'] = False
        out['error'] = '{}: {}'.format(exc.__class__.__name__, exc)
    return out


def host_state():
    """The host's own state: load, memory, the root and /var volumes."""
    out = {}
    try:
        with open('/proc/loadavg') as handle:
            parts = handle.read().split()
        out['load'] = {'1m': float(parts[0]), '5m': float(parts[1]),
                       '15m': float(parts[2])}
    except (OSError, ValueError, IndexError) as exc:
        out['load_error'] = str(exc)
    mem = {}
    try:
        with open('/proc/meminfo') as handle:
            for line in handle:
                key, _, rest = line.partition(':')
                if key in ('MemTotal', 'MemAvailable', 'SwapTotal',
                           'SwapFree'):
                    mem[key] = int(rest.split()[0])
        out['memory_kb'] = mem
    except (OSError, ValueError, IndexError) as exc:
        out['memory_error'] = str(exc)
    volumes = {}
    for mount in ('/', '/var'):
        try:
            st = os.statvfs(mount)
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            volumes[mount] = {
                'total_bytes': total, 'free_bytes': free,
                'used_percent': round(100.0 * (total - free) / total, 1)
                if total else None}
        except OSError as exc:
            volumes[mount] = {'error': str(exc)}
    out['volumes'] = volumes
    try:
        with open('/proc/uptime') as handle:
            out['uptime_seconds'] = int(float(handle.read().split()[0]))
    except (OSError, ValueError, IndexError) as exc:
        out['uptime_error'] = str(exc)
    return out


# ── The record ───────────────────────────────────────────────────────────

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


def collect(state):
    """The record this run delivers; ``state`` is advanced in place."""
    started = time.time()
    record = {'host': HOST, 'collected_at': now_iso(),
              'reporter_version': '1.0'}
    positions = state.setdefault('positions', {})
    last_run = state.get('last_run_at')
    first_run = not last_run
    record['interval'] = {'since': last_run,
                          'seconds': None, 'first_run': first_run}
    if last_run:
        try:
            since = datetime.strptime(last_run, '%Y-%m-%dT%H:%M:%SZ')
            record['interval']['seconds'] = int(
                (datetime.utcnow() - since).total_seconds())
        except ValueError:
            record['interval']['error'] = 'unparsable last_run_at'

    lines, info = read_since(ACCESS_LOG, positions, first_run)
    record['web_tier'] = web_tier(lines, info)
    record['web_tier']['limits'] = web_limits()
    lines, info = read_since(ERROR_LOG, positions, first_run)
    record['error_log'] = error_markers(lines, info)
    record['daemons'] = daemons()
    record['services'] = services()
    record['processes'] = processes()
    record['database'] = database()
    record['health'] = host_state()
    record['collect_seconds'] = round(time.time() - started, 2)
    state['last_run_at'] = record['collected_at']
    return record


# ── Delivery (the house pattern: env file, post, buffer) ─────────────────

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
    # host's trust store does not carry; the token is the authentication.
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
                        help='print the record, post nothing, leave the '
                             'state untouched')
    parser.add_argument('--env', default=os.path.expanduser(
        '~/.swf-panda-server-reporter.env'))
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
        print('post failed, record buffered: {}'.format(failure),
              file=sys.stderr)
        buffer_record(args.state, record)
        return 1
    web = record.get('web_tier') or {}
    print('reported: {} request(s), {} 5xx, {} error-log line(s), '
          'db connect {} ms'.format(
              web.get('requests'), web.get('status_5xx'),
              (record.get('error_log') or {}).get('lines'),
              (record.get('database') or {}).get('connect_ms', '?')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
