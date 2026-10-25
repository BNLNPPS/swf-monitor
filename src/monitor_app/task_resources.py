"""Twelve-hour observed job resource peaks, grouped by task and quarter hour."""
from datetime import datetime, timedelta, timezone

from django.db import connections, transaction

from .cached_product import get_product
from .panda.constants import PANDA_SCHEMA

PRODUCT_KEY = 'task_resource_peaks:v1'
BIN_SECONDS = 900


def assemble(rows, now):
    """Preserve missing telemetry as gaps; never substitute task resource requests."""
    first = int((now - timedelta(hours=12)).timestamp()) // BIN_SECONDS * BIN_SECONDS
    last = int(now.timestamp()) // BIN_SECONDS * BIN_SECONDS
    times = list(range(first, last + 1, BIN_SECONDS))
    tasks = {}
    for task_id, name, bucket, jobs, memory, disk, files, mem_n, disk_n, files_n in rows:
        task = tasks.setdefault(int(task_id), {'id': int(task_id), 'name': name or str(task_id),
                    'memory': [None] * len(times), 'disk': [None] * len(times),
                    'files': [None] * len(times), 'jobs': [0] * len(times),
                    'coverage': {'memory': 0, 'disk': 0, 'files': 0}})
        i = (int(bucket) - first) // BIN_SECONDS
        if not 0 <= i < len(times):
            continue
        task['jobs'][i] = int(jobs)
        for metric, value, count in [('memory', memory, mem_n), ('disk', disk, disk_n), ('files', files, files_n)]:
            task[metric][i] = float(value) if value is not None else None
            task['coverage'][metric] += int(count)
    return {'from': datetime.fromtimestamp(first, timezone.utc).isoformat(),
            'through': now.isoformat(), 'times': [datetime.fromtimestamp(t, timezone.utc).isoformat() for t in times],
            'bin_minutes': 15, 'tasks': sorted(tasks.values(), key=lambda t: t['id']),
            'units': {'memory': 'GiB', 'disk': 'GiB', 'files': 'files'},
            'basis': 'Maximum reported job peak in each 15-minute observation/completion bin; running jobs use their latest update.'}


def build():
    now = datetime.now(timezone.utc)
    start = datetime.fromtimestamp(int((now - timedelta(hours=12)).timestamp()) // BIN_SECONDS * BIN_SECONDS, timezone.utc)
    bounds = [start.replace(tzinfo=None), now.replace(tzinfo=None)]
    fields = '''pandaid,jeditaskid,jobstatus,COALESCE(endtime,modificationtime) observed,
                maxrss,jobmetrics'''
    sql = f'''WITH raw AS (
        SELECT {fields} FROM "{PANDA_SCHEMA}".jobsarchived4
          WHERE endtime >= %s AND endtime <= %s AND workinggroup='EIC'
        UNION ALL
        SELECT {fields} FROM "{PANDA_SCHEMA}".jobsactive4
          WHERE modificationtime >= %s AND modificationtime <= %s AND workinggroup='EIC'
            AND jobstatus IN ('running','starting','holding','finished','failed','closed','cancelled')
      ), jobs AS (
        SELECT DISTINCT ON (pandaid) * FROM raw ORDER BY pandaid, observed DESC
      ), measured AS (
        SELECT jeditaskid, FLOOR(EXTRACT(EPOCH FROM observed)/900)::bigint*900 bucket,
          CASE WHEN maxrss>0 THEN maxrss/1048576.0 END memory,
          substring(jobmetrics from '(?:^|[[:space:]])scratchPeakMB=([0-9]+)')::double precision/1024 disk,
          substring(jobmetrics from '(?:^|[[:space:]])scratchPeakFiles=([0-9]+)')::bigint files
        FROM jobs WHERE observed >= %s AND observed <= %s AND jeditaskid IS NOT NULL
      ) SELECT m.jeditaskid,t.taskname,m.bucket,COUNT(*),MAX(memory),MAX(disk),MAX(files),
          COUNT(memory),COUNT(disk),COUNT(files)
        FROM measured m LEFT JOIN "{PANDA_SCHEMA}".jedi_tasks t ON t.jeditaskid=m.jeditaskid
        GROUP BY m.jeditaskid,t.taskname,m.bucket ORDER BY m.jeditaskid,m.bucket'''
    with transaction.atomic(using='panda'):
        with connections['panda'].cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout='20s'")
            cursor.execute(sql, bounds * 3)
            rows = cursor.fetchall()
    return assemble(rows, now)


def product(refresh=False):
    return get_product(PRODUCT_KEY, build, 60, refresh=refresh, async_first_fill=True)
