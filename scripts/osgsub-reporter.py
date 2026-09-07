#!/usr/bin/env python3
"""osgsub-reporter.py — what only the OSG submit host can see.

Runs on osgsub01 and posts one record per run to swf-monitor
(docs/OSG_SUBMIT_REPORTER.md): the exclusions actually in force in the
submit descriptions, the queue-to-file mapping harvester uses, what the
pool currently offers, and what the local schedd is doing. The monitor
cannot read any of it — ssh to this host is refused except through the
facility gateway with agent forwarding — so the host pushes.

Written for this host as it is: python 3.6, standard library only, no
virtual environment. Every source that cannot be read is delivered as
an error field rather than dropped, because a missing field and an
unreadable source are otherwise the same thing to a reader.

Usage::

    osgsub-reporter.py                 # collect and post
    osgsub-reporter.py --print         # collect and print, post nothing
    osgsub-reporter.py --no-pool       # skip the condor queries

Configuration, from the environment or an environment file named by
--env (mode 600): SWF_MONITOR_URL, SWF_REPORT_TOKEN.
"""
from __future__ import print_function

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import time
from datetime import datetime

HOST = 'osgsub01'
SDF_DIR = '/var/data/atlpan/harvester_common'
QUEUE_CONFIG = '/opt/harvester/etc/panda/panda_queueconfig.json'
DEFAULT_STATE = os.path.expanduser('~/.swf-osgsub-reporter')
POST_TIMEOUT_S = 30
CONDOR_TIMEOUT_S = 120
BUFFER_KEEP = 48

# The node exclusions are written as one term per site inside a negated
# disjunction; this reads the pairs back out of whatever is deployed,
# so the record reflects the file rather than anything we believe.
PAIR_RE = re.compile(
    r'GLIDEIN_Site\s*=\?=\s*"([^"]+)"\s*&&\s*'
    r'regexp\(\s*"\^\(([^)]*)\)\(\[\.\]\|\$\)"',
    re.IGNORECASE)


def now_iso():
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')


def run(cmd, timeout=CONDOR_TIMEOUT_S):
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


def parse_sdf(path):
    """The exclusions and submission shape of one submit description."""
    out = {'file': os.path.basename(path)}
    try:
        with open(path) as handle:
            text = handle.read()
        out['modified'] = datetime.utcfromtimestamp(
            os.path.getmtime(path)).strftime('%Y-%m-%dT%H:%M:%SZ')
    except OSError as exc:
        out['error'] = 'unreadable: {}'.format(exc)
        return out

    sites = re.search(r'^\+UNDESIRED_Sites\s*=\s*"([^"]*)"', text, re.M)
    out['excluded_sites'] = (
        [s.strip() for s in sites.group(1).split(',') if s.strip()]
        if sites else [])

    req = re.search(r'^Requirements\s*=\s*(.+)$', text, re.M)
    out['requirements'] = req.group(1).strip() if req else None
    pairs = []
    if req:
        for site, names in PAIR_RE.findall(req.group(1)):
            for node in names.split('|'):
                if node.strip():
                    pairs.append({'site': site, 'node': node.strip()})
    out['excluded_site_nodes'] = pairs

    for field, pattern in (
            ('executable', r'^executable\s*=\s*(.+)$'),
            ('job_duration_category', r'^\+JobDurationCategory\s*=\s*"([^"]*)"'),
            ('request_cpus', r'^request_cpus\s*=\s*(.+)$'),
            ('request_memory', r'^request_memory\s*=\s*(.+)$'),
            ('request_disk', r'^request_disk\s*=\s*(.+)$')):
        found = re.search(pattern, text, re.M)
        out[field] = found.group(1).strip() if found else None
    return out


def submit_descriptions():
    """Every submit description on the host, parsed."""
    try:
        names = sorted(n for n in os.listdir(SDF_DIR) if n.endswith('.sdf'))
    except OSError as exc:
        return {'error': 'cannot list {}: {}'.format(SDF_DIR, exc)}
    return {n: parse_sdf(os.path.join(SDF_DIR, n)) for n in names}


def queue_map():
    """Which queue uses which submit description, and its worker limits."""
    try:
        with open(QUEUE_CONFIG) as handle:
            config = json.load(handle)
    except (OSError, ValueError) as exc:
        return {'error': 'cannot read {}: {}'.format(QUEUE_CONFIG, exc)}
    out = {}
    for queue, block in config.items():
        blob = json.dumps(block)
        found = re.findall(r'submit_pilot2[\w\-.]*\.sdf', blob)
        if not found:
            continue
        submitter = block.get('submitter') or {}
        out[queue] = {
            'submit_description': sorted(set(found))[0],
            'max_workers': submitter.get('maxWorkers'),
            'max_new_workers_per_cycle':
                submitter.get('maxNewWorkersPerCycle'),
            'workflow': block.get('workflow'),
        }
    return out


def pool(requirements, pairs):
    """What the pool offers, what the queue admits, what we exclude."""
    out = {}
    total, err = run(['condor_status', '-af', 'Name'])
    if err:
        return {'error': 'condor_status unavailable: {}'.format(err)}
    out['slots_total'] = len(total.strip().splitlines()) if total.strip() else 0

    if requirements:
        admitted, err = run(['condor_status', '-constraint', requirements,
                             '-af', 'Name'])
        out['slots_admitted'] = (
            len(admitted.strip().splitlines()) if admitted and admitted.strip()
            else (None if err else 0))
        if err:
            out['slots_admitted_error'] = err

    if pairs:
        terms = []
        for site in sorted(set(p['site'] for p in pairs)):
            names = '|'.join(p['node'] for p in pairs if p['site'] == site)
            terms.append('(GLIDEIN_Site =?= "{}" && regexp("^({})([.]|$)", '
                         'Machine) =?= True)'.format(site, names))
        banned = ' || '.join(terms)
        hit, err = run(['condor_status', '-constraint', banned,
                        '-af', 'Machine', 'GLIDEIN_Site'])
        if err:
            out['slots_excluded_error'] = err
        else:
            lines = [ln.strip() for ln in (hit or '').splitlines() if ln.strip()]
            out['slots_excluded_now'] = len(lines)
            out['excluded_present'] = sorted(set(lines))

    sites, err = run(['condor_status', '-af', 'GLIDEIN_Site'])
    if err:
        out['sites_error'] = err
    else:
        counts = {}
        for line in (sites or '').splitlines():
            name = line.strip() or 'unknown'
            counts[name] = counts.get(name, 0) + 1
        out['slots_by_site'] = counts
    return out


def schedd():
    """What the local schedd is holding, by queue where it is named."""
    out, err = run(['condor_q', '-all', '-af', 'JobStatus', 'HoldReason',
                    'MATCH_EXP_JOB_GLIDEIN_ResourceName'])
    if err:
        return {'error': 'condor_q unavailable: {}'.format(err)}
    states = {1: 'idle', 2: 'running', 5: 'held'}
    counts = {'idle': 0, 'running': 0, 'held': 0, 'other': 0}
    held_reasons = {}
    for line in (out or '').splitlines():
        parts = line.split(None, 1)
        if not parts:
            continue
        try:
            code = int(parts[0])
        except ValueError:
            continue
        counts[states.get(code, 'other')] = counts.get(
            states.get(code, 'other'), 0) + 1
        if code == 5 and len(parts) > 1:
            reason = parts[1].strip()[:300]
            held_reasons[reason] = held_reasons.get(reason, 0) + 1
    out_rec = {'workers': counts}
    if held_reasons:
        out_rec['held_reasons'] = held_reasons
    return out_rec


def collect(with_pool=True):
    """The record this run delivers."""
    record = {'host': HOST, 'collected_at': now_iso(),
              'reporter_version': '1.0'}
    started = time.time()
    record['submit_descriptions'] = submit_descriptions()
    record['queues'] = queue_map()

    # The production queues share one description; the pool figures are
    # taken against it, since that is what governs where pilots land.
    prod = (record['submit_descriptions'] or {}).get(
        'submit_pilot2_push_bnl_osg.sdf') or {}
    record['exclusions'] = {
        'submit_description': prod.get('file'),
        'modified': prod.get('modified'),
        'sites': prod.get('excluded_sites'),
        'site_nodes': prod.get('excluded_site_nodes'),
        'queues': sorted(q for q, v in (record['queues'] or {}).items()
                         if isinstance(v, dict)
                         and v.get('submit_description')
                         == 'submit_pilot2_push_bnl_osg.sdf'),
    }
    if with_pool:
        record['pool'] = pool(prod.get('requirements'),
                              prod.get('excluded_site_nodes') or [])
        record['schedd'] = schedd()
    record['collect_seconds'] = round(time.time() - started, 2)
    return record


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
                        help='print the record, post nothing')
    parser.add_argument('--no-pool', dest='pool', action='store_false',
                        help='skip the condor queries')
    parser.add_argument('--env', default=os.path.expanduser(
        '~/.swf-osgsub-reporter.env'))
    parser.add_argument('--state', default=DEFAULT_STATE)
    args = parser.parse_args()

    record = collect(with_pool=args.pool)
    if args.show:
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0

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
    excl = record.get('exclusions') or {}
    print('reported: {} site(s), {} node(s) excluded, {} slot(s) removed now'
          .format(len(excl.get('sites') or []),
                  len(excl.get('site_nodes') or []),
                  (record.get('pool') or {}).get('slots_excluded_now', '?')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
