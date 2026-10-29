#!/usr/bin/env python
"""Backtest the node guard's decision over past windows (site-canary
docs/NODE_GUARD.md, What the backtest measured).

Steps the guard's window back over ``--days`` at ``--step-h``, reads the
terminal production jobs of each window from PanDA once, and runs
``canary.guard.decide_nodes`` with the live settings (SysConfig
``node_guard.*``), the attribution built from the same record over
``attribution_h`` before each window's end. Prints every trip by reason,
queue and host with its windows and peak evidence; ``--reason`` limits
the listing. Read-only: no record, document or action is written.

    python scripts/node-guard-backtest.py --days 7 --reason outlier
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import timedelta, timezone as dt_timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
import django  # noqa: E402

django.setup()

from django.db import connections  # noqa: E402
from django.utils import timezone  # noqa: E402

from canary import guard  # noqa: E402
from monitor_app.panda import node_guard  # noqa: E402
from monitor_app.panda.constants import PANDA_SCHEMA  # noqa: E402


def read_jobs(start, end):
    """Every terminal production job that ended in [start, end), in the
    window_rows shape plus the end as a datetime."""
    sql = f"""
        SELECT "computingsite", "modificationhost", "jobstatus", "jeditaskid",
               EXTRACT(EPOCH FROM ("endtime" - "starttime")), "endtime", "pandaid",
               "destinationsite", "piloterrorcode", "exeerrorcode", "transexitcode"
        FROM "{PANDA_SCHEMA}"."jobsarchived4"
        WHERE "processingtype" = 'epicproduction'
          AND "endtime" >= %s AND "endtime" < %s
          AND "jobstatus" IN ('finished', 'failed')
    """
    with connections['panda'].cursor() as cursor:
        cursor.execute(sql, [start.replace(tzinfo=None), end.replace(tzinfo=None)])
        rows = cursor.fetchall()
    out = []
    for (queue, host, status, task, seconds, ended, pandaid, dest, pilot, exe, trans) in rows:
        codes = (str(exe or '').strip(), str(trans or '').strip())
        out.append({'queue': str(queue) if queue else None, 'host': host,
                    'jobstatus': status, 'jeditaskid': task,
                    'duration_s': float(seconds) if seconds is not None else None,
                    'endtime': ended.isoformat() if ended is not None else None,
                    'ended': ended, 'pandaid': pandaid,
                    'site': node_guard.execute_site(dest, host),
                    'error': node_guard.error_label(pilot, exe, trans) if status == 'failed' else '',
                    'registration': status == 'failed' and '78' in codes,
                    'declined': status == 'failed' and '80' in codes})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--days', type=float, default=7)
    ap.add_argument('--step-h', type=float, default=1)
    ap.add_argument('--reason', default='', help='list only trips with this reason')
    args = ap.parse_args()

    # The live settings read without seeding (get_setting writes a missing
    # key); a key not yet in SysConfig takes its code default.
    from monitor_app.models import SysConfig
    live = SysConfig.get_config()
    cfg = {k: live.get(f'node_guard.{k}', d) for k, d in node_guard.DEFAULTS.items()}
    decision = {k: cfg[k] for k in node_guard.DECISION_KEYS}
    window = timedelta(hours=float(cfg['window_h']))
    attribution = timedelta(hours=float(cfg['attribution_h']))
    now = timezone.now()
    first_end = now - timedelta(days=args.days)
    jobs = read_jobs(first_end - window - attribution, now)
    jobs = [j for j in jobs if j['ended'] is not None]
    for j in jobs:
        if timezone.is_naive(j['ended']):
            j['ended'] = timezone.make_aware(j["ended"], dt_timezone.utc)
    calib = node_guard.median_finished_s()
    print(f'{len(jobs)} terminal production jobs read; windows of {cfg["window_h"]} h '
          f'every {args.step_h} h over {args.days} days', file=sys.stderr)

    trips = defaultdict(list)
    end = first_end
    while end <= now:
        rows = [j for j in jobs if end - window < j['ended'] <= end]
        elsewhere = defaultdict(lambda: defaultdict(dict))
        for j in jobs:
            if j['jobstatus'] == 'finished' and j['jeditaskid'] is not None \
                    and end - attribution < j['ended'] <= end:
                host = guard.normalize_host(j['host'])
                prev = elsewhere[j['queue']][j['jeditaskid']].get(host)
                if prev is None or j['endtime'] > prev:
                    elsewhere[j['queue']][j['jeditaskid']][host] = j['endtime']
        out = guard.decide_nodes(rows, calib, decision, finished_elsewhere=elsewhere)
        for queue, host in out['tripped']:
            v = out['queues'][queue]['nodes'][host]
            if args.reason and v['reason'] != args.reason:
                continue
            e = v['evidence']
            trips[(v['reason'], queue, host)].append(
                (timezone.localtime(end).strftime('%m-%d %H:%M'), e['jobs'], e['failed'],
                 e['finished'], e.get('queue_others_failed_fraction'), e.get('outlier_ratio')))
        end += timedelta(hours=args.step_h)

    if not trips:
        print('no trips')
    for (reason, queue, host), hits in sorted(trips.items()):
        peak = max(hits, key=lambda h: h[2])
        print(f'{reason:12} {queue:28} {host:24} windows {len(hits):3}  '
              f'{hits[0][0]} .. {hits[-1][0]} ET  peak: jobs {peak[1]} failed {peak[2]} '
              f'finished {peak[3]} others_failed {peak[4]} ratio {peak[5]}')


if __name__ == '__main__':
    main()
