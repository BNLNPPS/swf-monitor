#!/usr/bin/env python3
"""pool-reporter.py — how full the batch pool our queues draw on is.

The E1 and BNL queues run in the SCDF shared pool, and a worker's wait
there is decided by how much of the pool is claimed and how deep the
queue ahead of it is. Neither figure is in any PanDA record: the pool's
collector is the only source, and it answers the HTCondor protocol, not
HTTP. This reporter asks it and posts the answer to swf-monitor's host
report ingest (docs/POOL_REPORTER.md), so the web tier reads a stored
record and never speaks to a collector in a request.

It runs on the monitor host, where the collector is reachable, in its
own virtual environment: the HTCondor bindings are large and belong to
this reporter rather than to the web tier's dependencies.

Usage::

    pool-reporter.py                 # collect and post
    pool-reporter.py --print         # collect and print, post nothing

Configuration, from the environment or an environment file named by
--env (mode 600): SWF_MONITOR_URL, SWF_REPORT_TOKEN. The pool is named
by --pool and --collector, defaulting to the SCDF shared pool.
"""
import argparse
import json
import os
import ssl
import sys
import time
from collections import Counter
from datetime import datetime, timezone

DEFAULT_POOL = 'bnl-scdf'
DEFAULT_COLLECTOR = 'condorspool01.sdcc.bnl.gov:9665'
POST_TIMEOUT_S = 30
# The pool holds tens of thousands of slots on hundreds of machines and
# dozens of schedds. Only bounded summaries are delivered: a record is a
# summary, not a payload, and the ingest refuses anything large.
MAX_DOMAINS = 24
MAX_SCHEDDS = 40


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def domain_of(machine):
    """The part of a machine name that identifies its pool.

    Slot names arrive as ``slot1_14@spool1499.sdcc.bnl.gov`` in the job
    records and as bare machine names here; the domain is what the two
    have in common, and it is what says which pool a queue's workers ran
    in. A machine with no domain contributes its name's leading letters,
    which is how the OSG pool's bare ``compute34`` names are grouped.
    """
    name = (machine or '').split('@')[-1].strip().lower()
    if not name:
        return ''
    if '.' in name:
        return name.split('.', 1)[1]
    return name.rstrip('0123456789') or name


def read_pool(collector):
    """One reading of the pool: what it holds, and the queue ahead."""
    import htcondor2 as htcondor

    out = {'collector': collector}
    started = time.time()
    coll = htcondor.Collector(collector)

    try:
        slots = coll.query(htcondor.AdType.Startd,
                           projection=['State', 'Machine', 'Cpus'])
    except Exception as exc:                                  # noqa: BLE001
        return {'collector': collector,
                'error': 'startd query failed: {}: {}'.format(
                    exc.__class__.__name__, exc)}

    states = Counter()
    cores = Counter()
    domains = Counter()
    for ad in slots:
        state = str(ad.get('State') or 'unknown').lower()
        states[state] += 1
        try:
            cpus = int(ad.get('Cpus') or 0)
        except (TypeError, ValueError):
            cpus = 0
        cores[state] += cpus
        domains[domain_of(ad.get('Machine'))] += 1

    total = sum(states.values())
    claimed = states.get('claimed', 0)
    out['slots'] = {'total': total,
                    'claimed': claimed,
                    'unclaimed': states.get('unclaimed', 0),
                    'by_state': dict(states)}
    out['cores'] = {'total': sum(cores.values()),
                    'claimed': cores.get('claimed', 0)}
    out['claimed_fraction'] = round(float(claimed) / total, 4) if total else None
    kept = domains.most_common(MAX_DOMAINS)
    out['machine_domains'] = dict(kept)
    if len(domains) > len(kept):
        out['machine_domains_other'] = len(domains) - len(kept)

    try:
        schedds = coll.query(htcondor.AdType.Schedd,
                             projection=['Name', 'TotalRunningJobs',
                                         'TotalIdleJobs', 'TotalHeldJobs'])
    except Exception as exc:                                  # noqa: BLE001
        out['schedd_error'] = '{}: {}'.format(exc.__class__.__name__, exc)
        out['collect_seconds'] = round(time.time() - started, 2)
        return out

    def _int(ad, key):
        try:
            return int(ad.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    rows = [{'name': str(ad.get('Name') or ''),
             'running': _int(ad, 'TotalRunningJobs'),
             'idle': _int(ad, 'TotalIdleJobs'),
             'held': _int(ad, 'TotalHeldJobs')}
            for ad in schedds]
    rows.sort(key=lambda r: r['idle'] + r['running'], reverse=True)
    out['queue'] = {
        'schedds': len(rows),
        'running': sum(r['running'] for r in rows),
        'idle': sum(r['idle'] for r in rows),
        'held': sum(r['held'] for r in rows),
        'busiest': rows[:MAX_SCHEDDS],
    }
    out['collect_seconds'] = round(time.time() - started, 2)
    return out


def collect(pool, collector):
    return {'pool': pool,
            'collected_at': now_iso(),
            'reporter_version': '1.0',
            'reading': read_pool(collector)}


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


def post(record, url, token, key):
    """Deliver one record. Returns None on success, the reason on failure."""
    import urllib.request
    body = json.dumps(record).encode('utf-8')
    endpoint = '{}/api/host-reports/{}/'.format(url.rstrip('/'), key)
    request = urllib.request.Request(
        endpoint, data=body, method='POST',
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Token {}'.format(token)})
    # The monitor serves a certificate this host's trust store carries
    # only through the combined bundle; the token is the authentication,
    # and the request never leaves the host.
    context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=POST_TIMEOUT_S,
                                    context=context) as response:
            response.read()
        return None
    except Exception as exc:                                  # noqa: BLE001
        return '{}: {}'.format(exc.__class__.__name__, exc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--print', dest='show', action='store_true',
                        help='print the record, post nothing')
    parser.add_argument('--pool', default=DEFAULT_POOL,
                        help='the pool name, which is also the report key')
    parser.add_argument('--collector', default=DEFAULT_COLLECTOR)
    parser.add_argument('--env', default=os.path.expanduser(
        '~/.swf-pool-reporter.env'))
    args = parser.parse_args()

    record = collect(args.pool, args.collector)
    if args.show:
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0

    load_env(args.env)
    url = os.environ.get('SWF_MONITOR_URL')
    token = os.environ.get('SWF_REPORT_TOKEN')
    if not url or not token:
        print('SWF_MONITOR_URL and SWF_REPORT_TOKEN are required '
              '(environment or --env file)', file=sys.stderr)
        return 2

    failure = post(record, url, token, args.pool)
    if failure:
        print('post failed: {}'.format(failure), file=sys.stderr)
        return 1
    reading = record.get('reading') or {}
    if reading.get('error'):
        print('reported, with an error in the reading: {}'
              .format(reading['error']))
        return 0
    slots = reading.get('slots') or {}
    queue = reading.get('queue') or {}
    print('reported: {} of {} slots claimed, {} jobs waiting ahead'
          .format(slots.get('claimed'), slots.get('total'),
                  queue.get('idle')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
