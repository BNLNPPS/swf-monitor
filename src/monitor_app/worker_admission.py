"""Worker-start cohorts and native pilot admission evidence."""
from collections import Counter
from datetime import timedelta

from django.db import connections
from django.utils import timezone

from .cached_product import get_product
from .host_reports import latest


def outcome(queue, status, exit_code):
    """Keep deliberate native pre-job refusals apart from failed payloads."""
    try:
        exit_code = int(exit_code) if exit_code is not None else None
    except (TypeError, ValueError):
        exit_code = None
    if queue == 'BNL_OSG_PanDA_1' and exit_code == 73:
        return 'declined'
    if status == 'cancelled':
        return 'cancelled'
    if status == 'finished' and exit_code not in (None, 0):
        return 'failed'
    return status or 'unknown'


def build(queues, hours):
    end = timezone.now().replace(tzinfo=None)
    start = end - timedelta(hours=hours)
    cohorts = Counter()
    declines_seconds = 0
    reasons = []
    recent = []
    with connections['panda'].cursor() as cursor:
        cursor.execute('''SELECT computingsite, status, nativeexitcode, COUNT(*),
            SUM(GREATEST(0, EXTRACT(EPOCH FROM (endtime-starttime))))
            FROM doma_panda.harvester_workers
            WHERE computingsite = ANY(%s) AND starttime > %s AND starttime <= %s
            GROUP BY 1, 2, 3''', [queues, start, end])
        for queue, status, code, count, seconds in cursor.fetchall():
            label = outcome(queue, status, code)
            cohorts[label] += count
            if label == 'declined':
                declines_seconds += float(seconds or 0)
            reasons.append({'outcome': label, 'raw_status': status,
                            'exit_code': code, 'count': count})
        cursor.execute('''SELECT status, COUNT(*) FROM doma_panda.harvester_workers
            WHERE computingsite = ANY(%s) AND status IN ('running','submitted','ready','idle')
            GROUP BY 1''', [queues])
        workers_now = dict(cursor.fetchall())
        cursor.execute('''SELECT COUNT(*) FROM doma_panda.jobsactive4
            WHERE computingsite = ANY(%s) AND jobstatus='running' ''', [queues])
        jobs_now = cursor.fetchone()[0]
        cursor.execute('''SELECT workerid, harvesterid, batchid, computingsite,
            status, nativeexitcode, starttime, endtime, batchlog, diagmessage
            FROM doma_panda.harvester_workers
            WHERE computingsite = ANY(%s) AND starttime > %s AND starttime <= %s
            ORDER BY starttime DESC LIMIT 30''', [queues, start, end])
        for worker, harvester, batch, queue, status, code, began, ended, log, diag in cursor.fetchall():
            label = outcome(queue, status, code)
            recent.append({'workerid': worker, 'harvesterid': harvester,
                'batchid': batch, 'queue': queue, 'outcome': label,
                'exit_code': code, 'started': began.isoformat() if began else None,
                'ended': ended.isoformat() if ended else None, 'batch_log': log,
                'reason': ('Admission declined before taking a job: insufficient remaining lifetime'
                           if label == 'declined' else str(diag or ''))})
    starts = sum(cohorts.values())
    return {'since': start.isoformat() + 'Z', 'until': end.isoformat() + 'Z',
        'hours': hours, 'starts': starts, 'declined': cohorts['declined'],
        'declined_pct': round(100 * cohorts['declined'] / starts, 1) if starts else 0,
        'still_running': cohorts['running'], 'finished': cohorts['finished'],
        'failed': cohorts['failed'], 'cancelled': cohorts['cancelled'],
        'other': sum(value for key, value in cohorts.items()
                     if key not in ('declined','running','finished','failed','cancelled')),
        'declined_worker_hours': round(declines_seconds / 3600, 1),
        'workers_now': workers_now, 'jobs_now': jobs_now,
        'outcomes': sorted(reasons, key=lambda row: -row['count']), 'recent': recent}


def product(queues, hours=1, refresh=False):
    key = 'worker_admission:v1:' + ','.join(sorted(queues)) + ':' + str(hours)
    return get_product(key, lambda: build(queues, hours), ttl_seconds=60,
                       refresh=refresh)


def startup_evidence(queues):
    for host in ('osgsub01-harvester', 'pandaharvester01', 'pandaharvester02'):
        report = latest(host) or {}
        data = ((report.get('record') or {}).get('harvester_db') or {}).get('admission_declines')
        if data and 'BNL_OSG_PanDA_1' in queues:
            return dict(data, reported_at=report.get('reported_at'),
                        age_seconds=report.get('age_seconds'), host=host)
    return None
