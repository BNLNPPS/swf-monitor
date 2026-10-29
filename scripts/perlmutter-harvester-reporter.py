#!/usr/bin/env python3
"""perlmutter-harvester-reporter.py — the Perlmutter harvester's record.

The harvester ``Perlmutter_test_1`` runs on a NERSC login node under PanDA
operations' account, and its logs are in that account's scratch, which the
m3763 project cannot read. What it reports to the PanDA server (its
heartbeat, its workers, its worker counts and intentions, its errors and
its metrics) and its queue configuration, which m3763 reads through the
NERSC IRI API, make the record the other harvesters' reporters deliver
(docs/HARVESTER_REPORTER.md, Perlmutter), under the host key
``perlmutter-harvester``. Read-only with respect to the harvester.

Runs on pandaserver02 every five minutes from the deployed release, which
stores the record through ``host_reports.store``. A source that cannot be
read is an error field, never dropped. Times are UTC, as in every host
record.

    cd /opt/swf-monitor/current && source ~/.env
    .venv/bin/python scripts/perlmutter-harvester-reporter.py [--print]

``--print`` prints the record and stores nothing; a development tree
always only prints (CACHED_PRODUCTS.md, Contract).
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from django.db import connections  # noqa: E402

HOST_KEY = 'perlmutter-harvester'
HARVESTER_ID = 'Perlmutter_test_1'
VERSION = '1.1'
SCHEMA = 'doma_panda'
QUEUE_CONFIG = '/global/common/software/m3763/panda-harvester/etc/panda/panda_queueconfig.json'
STATE_DIR = os.path.expanduser('~/.swf-harvester-reporter-' + HOST_KEY)
TERMINAL = ('finished', 'failed', 'cancelled', 'missed')
# A worker row not terminal after this long is one the harvester lost
# track of, not a live worker (comp_snapshot workers_now uses the same).
LIVE_DAYS = 7
REASON_HOURS = 24
DIALOG_HOURS = 24
EXAMPLES = 3
REASONS_KEEP = 25
LINE_KEEP = 400
LOGS = ("the harvester's logs are in PanDA operations' scratch at NERSC "
        "(/pscratch/sd/x/xin/panda/log), which m3763 cannot read")
FETCH = ('not applicable: the Perlmutter queues run in pull mode (mapType NoJob), '
         'so their pilots fetch their own jobs; ' + LOGS)


def rows(sql, params=()):
    with connections['panda'].cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def iso(t):
    return t.strftime('%Y-%m-%dT%H:%M:%SZ') if t else None


def error(exc):
    return {'error': '{}: {}'.format(exc.__class__.__name__, str(exc)[:LINE_KEEP])}


def instance(now):
    found = rows(f'''SELECT starttime, lastupdate, hostname, sw_version, owner
                     FROM {SCHEMA}.harvester_instances WHERE harvester_id = %s''', (HARVESTER_ID,))
    if not found:
        return {'error': f'no {HARVESTER_ID} in harvester_instances'}
    start, last, host, version, owner = found[0]
    return {'harvester_id': HARVESTER_ID, 'started_at': iso(start), 'last_heartbeat': iso(last),
            'heartbeat_age_s': int((now - last).total_seconds()) if last else None,
            'hostname': host or None, 'sw_version': version, 'owner': owner}


def queue_config():
    """The queue configuration through the IRI API: each queue's launch
    limits, in the harvester database's names, and its queueStatus."""
    from swf_epicprod.nersc_iri import Client
    config = json.loads(Client().read(QUEUE_CONFIG))
    blocks = {q: b for q, b in config.items() if isinstance(b, dict)}
    limits = [{'queue': q, 'site': q, 'job_type': None, 'resource_type': None,
               'n_queue_limit_worker': b.get('nQueueLimitWorker'),
               'max_workers': b.get('maxWorkers'),
               'n_queue_limit_job': b.get('nQueueLimitJob'),
               'n_queue_limit_worker_ratio': b.get('nQueueLimitWorkerRatio'),
               'n_queue_limit_worker_min': b.get('nQueueLimitWorkerMin'),
               'n_queue_limit_worker_max': b.get('nQueueLimitWorkerMax'),
               'map_type': b.get('mapType')}
              for q, b in sorted(blocks.items())]
    return limits, {'status': {q: b.get('queueStatus') for q, b in blocks.items()}}


def harvester_db(since, until, now, limits):
    """The harvester's workers as it reports them to the PanDA server, in
    the shape the other reporters read from the harvester's own database."""
    out = {'window': {'since': since.strftime('%Y-%m-%d %H:%M:%S'),
                      'until': until.strftime('%Y-%m-%d %H:%M:%S')},
           'source': 'the PanDA server tables the harvester reports to'}
    try:
        live = now - timedelta(days=LIVE_DAYS)
        found = rows(f'''SELECT computingsite, status, COUNT(*) FROM {SCHEMA}.harvester_workers
                         WHERE harvesterid = %s AND status <> ALL(%s) AND submittime >= %s
                         GROUP BY 1, 2''', (HARVESTER_ID, list(TERMINAL), live))
        workers = {}
        for site, status, n in found:
            workers.setdefault(site or '', {})[status] = int(n)
        out['workers_now'] = workers
    except Exception as exc:  # noqa: BLE001
        out['workers_now'] = error(exc)
    try:
        found = rows(f'''SELECT computingsite, SUM(n_workers), MAX(lastupdate)
                         FROM {SCHEMA}.harvester_worker_stats
                         WHERE harvester_id = %s AND status = 'to_submit' GROUP BY 1''', (HARVESTER_ID,))
        out['workers_to_submit'] = {site: {'n': int(n or 0), 'at': iso(at)} for site, n, at in found}
    except Exception as exc:  # noqa: BLE001
        out['workers_to_submit'] = error(exc)
    if isinstance(limits, list):
        to_submit = out.get('workers_to_submit') if isinstance(out.get('workers_to_submit'), dict) else {}
        last = dict(rows(f'''SELECT computingsite, MAX(submittime) FROM {SCHEMA}.harvester_workers
                              WHERE harvesterid = %s AND submittime >= %s GROUP BY 1''',
                         (HARVESTER_ID, now - timedelta(days=LIVE_DAYS))))
        for row in limits:
            row['n_new_workers'] = (to_submit.get(row['queue']) or {}).get('n')
            row['submit_time'] = (last[row['queue']].strftime('%Y-%m-%d %H:%M:%S')
                                  if last.get(row['queue']) else None)
            row['job_fetch_time'] = None
        out['launch_limits'] = limits
    else:
        out['launch_limits'] = limits
    try:
        found = rows(f'''SELECT computingsite, COUNT(*) FROM {SCHEMA}.harvester_workers
                         WHERE harvesterid = %s AND starttime > %s AND starttime <= %s GROUP BY 1''',
                     (HARVESTER_ID, since, until))
        started = {site: int(n) for site, n in found}
        for site in out.get('workers_now') or {}:
            if site != 'error':
                started.setdefault(site, 0)
        out['workers_started'] = started
    except Exception as exc:  # noqa: BLE001
        out['workers_started'] = error(exc)
    try:
        found = rows(f'''SELECT computingsite, status, EXTRACT(EPOCH FROM (endtime - starttime))
                         FROM {SCHEMA}.harvester_workers
                         WHERE harvesterid = %s AND endtime > %s AND endtime <= %s''',
                     (HARVESTER_ID, since, until))
        sites = {}
        for site, status, run_s in found:
            s = sites.setdefault(site or '', {'ended': 0, 'by_status': {}, '_runs': []})
            s['ended'] += 1
            s['by_status'][status] = s['by_status'].get(status, 0) + 1
            if status == 'finished' and run_s is not None and run_s >= 0:
                s['_runs'].append(int(run_s))
        for s in sites.values():
            runs = sorted(s.pop('_runs'))
            s['median_finished_run_s'] = runs[len(runs) // 2] if runs else None
        out['workers_ended'] = {'sites': sites, 'workers': len(found)}
    except Exception as exc:  # noqa: BLE001
        out['workers_ended'] = error(exc)
    try:
        out['not_finished'] = not_finished(until)
    except Exception as exc:  # noqa: BLE001
        out['not_finished'] = error(exc)
    try:
        found = rows(f'''SELECT DISTINCT ON (harvester_host) harvester_host, creation_time, metrics
                         FROM {SCHEMA}.harvester_metrics WHERE harvester_id = %s
                         ORDER BY harvester_host, creation_time DESC''', (HARVESTER_ID,))
        metrics = {}
        for host, created, raw in found:
            try:
                value = json.loads(raw) if raw else None
            except ValueError:
                value = (raw or '')[:2000]
            metrics[host or ''] = {'at': iso(created), 'metrics': value}
        out['service_metrics'] = metrics
    except Exception as exc:  # noqa: BLE001
        out['service_metrics'] = error(exc)
    out['cache_updates'] = {'error': "the harvester's job-demand cache is in its own database "
                                     "and does not reach the PanDA server"}
    return out


def not_finished(until):
    """Per site, over the REASON_HOURS before ``until``, every worker that
    did not finish or finished with a nonzero batch exit, grouped by the
    harvester's diagnostic, as the other reporters group theirs."""
    found = rows(f'''SELECT workerid, batchid, computingsite, status, nativestatus, endtime,
                            LEFT(diagmessage, 300), COALESCE(batchlog, stdout), nodeid,
                            computingelement, nativeexitcode
                     FROM {SCHEMA}.harvester_workers
                     WHERE harvesterid = %s AND endtime > %s AND endtime <= %s
                       AND (status <> 'finished' OR COALESCE(nativeexitcode, 0) <> 0)
                     ORDER BY endtime DESC''',
                 (HARVESTER_ID, until - timedelta(hours=REASON_HOURS), until))
    sites = {}
    for worker, batch, site, status, native, ended, diag, log, node, ce, _ in found:
        text = (diag or '').strip() or (native or '').strip() or status
        s = sites.setdefault(site or '', {'workers': 0, 'reasons': {}})
        s['workers'] += 1
        entry = s['reasons'].setdefault(f'{status} | {text}', {
            'status': status, 'native_status': (native or '').strip() or None,
            'source': 'harvester diagnostic', 'event': None, 'reason': text, 'count': 0,
            'last_ended': ended.strftime('%Y-%m-%d %H:%M:%S') if ended else None, 'examples': []})
        entry['count'] += 1
        if len(entry['examples']) < EXAMPLES:
            entry['examples'].append({'workerid': int(worker), 'batchid': batch,
                                      'ended': ended.strftime('%Y-%m-%d %H:%M:%S') if ended else None,
                                      'node': node, 'ce': ce, 'batch_log': log})
    for s in sites.values():
        reasons = sorted(s['reasons'].values(), key=lambda e: -e['count'])
        s['reasons'] = reasons[:REASONS_KEEP]
        s['reasons_dropped'] = max(0, len(reasons) - REASONS_KEEP)
    return {'hours': REASON_HOURS, 'sites': sites, 'workers': sum(v['workers'] for v in sites.values())}


def submission(since, until):
    found = rows(f'''SELECT computingsite, COUNT(*) FROM {SCHEMA}.harvester_workers
                     WHERE harvesterid = %s AND submittime > %s AND submittime <= %s GROUP BY 1''',
                 (HARVESTER_ID, since, until))
    return {'queues': {site: {'workers_submitted': int(n)} for site, n in found},
            'source': 'the workers the harvester reported to the PanDA server'}


def dialogs(now):
    """The errors the harvester reported to the PanDA server over the last
    DIALOG_HOURS, per module (count and the latest message) and per kind
    of message (the message with its numbers set aside), so one kind's
    latest line never hides another's. The page shows a queue the kinds
    that name it or name none of the harvester's queues."""
    found = rows(f'''SELECT modulename, messagelevel, creationtime, LEFT(diagmessage, {LINE_KEEP})
                     FROM {SCHEMA}.harvester_dialogs
                     WHERE harvester_id = %s AND creationtime > %s
                       AND messagelevel IN ('ERROR', 'CRITICAL')
                     ORDER BY creationtime DESC''', (HARVESTER_ID, now - timedelta(hours=DIALOG_HOURS)))
    modules, kinds = {}, {}
    for module, level, at, message in found:
        m = modules.setdefault(module or '', {'count': 0, 'levels': {},
                                              'last': {'at': iso(at), 'level': level, 'message': message}})
        m['count'] += 1
        m['levels'][level] = m['levels'].get(level, 0) + 1
        k = kinds.setdefault((module or '', re.sub(r'\d+', '#', message or '')),
                             {'module': module or '', 'count': 0, 'levels': {}, 'first_at': None,
                              'last': {'at': iso(at), 'level': level, 'message': message}})
        k['count'] += 1
        k['levels'][level] = k['levels'].get(level, 0) + 1
        k['first_at'] = iso(at)
    messages = sorted(kinds.values(), key=lambda k: k['last']['at'] or '', reverse=True)
    return {'hours': DIALOG_HOURS, 'modules': modules, 'messages': messages[:REASONS_KEEP],
            'messages_dropped': max(0, len(messages) - REASONS_KEEP)}


def load_state():
    try:
        with open(os.path.join(STATE_DIR, 'state.json')) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, 'state.json')
    with open(path + '.new', 'w') as handle:
        json.dump(state, handle)
    os.replace(path + '.new', path)


def collect(state):
    started = time.time()
    now = datetime.utcnow().replace(microsecond=0)
    last = state.get('last_run_at')
    since = now - timedelta(minutes=5)
    if last:
        try:
            since = datetime.strptime(last, '%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            pass
    record = {'host': HOST_KEY, 'harvester_id': HARVESTER_ID, 'collected_at': iso(now),
              'reporter_version': VERSION,
              'source': ('what the harvester reports to the PanDA server, and its queue '
                         'configuration through the NERSC IRI API; ' + LOGS),
              'interval': {'since': last, 'seconds': int((now - since).total_seconds()),
                           'first_run': not last}}
    for name, read in (('instance', lambda: instance(now)), ('dialogs', lambda: dialogs(now)),
                       ('submission', lambda: submission(since, now))):
        try:
            record[name] = read()
        except Exception as exc:  # noqa: BLE001
            record[name] = error(exc)
    try:
        limits, record['queue_status'] = queue_config()
    except Exception as exc:  # noqa: BLE001
        limits = error(exc)
        record['queue_status'] = error(exc)
    record['harvester_db'] = harvester_db(since, now, now, limits)
    record['fetching'] = {'error': FETCH}
    record['adjustment'] = {'queues': {}, 'read': {'error': LOGS}}
    record['failed_calls'] = {'error': LOGS}
    record['daemons'] = {'error': LOGS}
    record['processes'] = {'error': "the harvester runs on a NERSC login node, whose process "
                                    "table m3763 cannot read; its heartbeat is in instance"}
    record['collect_seconds'] = round(time.time() - started, 2)
    state['last_run_at'] = record['collected_at']
    return record


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--print', action='store_true', help='print the record; store nothing')
    args = ap.parse_args()
    from monitor_app.cached_product import stores
    from monitor_app.host_reports import RecordRefused, store
    if args.print or not stores():
        print(json.dumps(collect(load_state()), indent=1, default=str))
        return 0
    state = load_state()
    record = collect(state)
    try:
        at, size = store(HOST_KEY, record, by='perlmutter-harvester-reporter')
    except RecordRefused as exc:
        print(f'{iso(datetime.utcnow())} refused: {exc}')
        return 1
    save_state(state)
    errors = [k for k, v in record.items() if isinstance(v, dict) and v.get('error')
              and k not in ('fetching', 'failed_calls', 'daemons', 'processes')]
    print(f"{record['collected_at']} stored {size} bytes"
          + (f"; unread: {', '.join(errors)}" if errors else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
