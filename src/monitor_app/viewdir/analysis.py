"""The Analysis page: user analysis on the production platform.

In-development landing page for the analysis capability
(swf-epicprod/docs/PANDA_USER_JOBS.md): the analysis-capable queue set,
analysis task activity, and analysis weather (queue wait times).
Weather is computed from PanDA accounting data and cached; the page
never runs the percentile scan on a warm cache.
"""
from django.core.cache import cache
from django.db import connections
from django.shortcuts import render

from ..panda import list_queues

WEATHER_CACHE_KEY = 'analysis_weather_v1'
WEATHER_CACHE_TTL = 3600  # seconds; hourly refresh is ample for a 14-day window
WEATHER_WINDOW_DAYS = 14


def _analysis_queues():
    """Unified queues from live PanDA schedconfig (the source the EIC
    queues page uses) — the queues serving both job classes."""
    result = list_queues(vo='eic')
    return [q for q in result.get('queues', [])
            if q.get('type') == 'unified']


def _analysis_activity(limit=100):
    """Recent user-label tasks across the instance (PanDA DB)."""
    cur = connections['panda'].cursor()
    cur.execute(
        "SELECT jeditaskid, taskname, username, status, creationdate, "
        "modificationtime FROM doma_panda.jedi_tasks "
        "WHERE prodsourcelabel = 'user' "
        "AND modificationtime > now() - interval '30 days' "
        "ORDER BY jeditaskid DESC LIMIT %s", [limit])
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _analysis_weather():
    """Per-queue job wait profile (creation to start), cached hourly."""
    rows = cache.get(WEATHER_CACHE_KEY)
    if rows is not None:
        return rows
    cur = connections['panda'].cursor()
    cur.execute(
        "SELECT computingsite, count(*) AS jobs, "
        "round((percentile_cont(0.5) WITHIN GROUP (ORDER BY "
        "EXTRACT(EPOCH FROM (starttime - creationtime))/60.0))::numeric, 1) "
        "AS median_wait_min, "
        "round((percentile_cont(0.9) WITHIN GROUP (ORDER BY "
        "EXTRACT(EPOCH FROM (starttime - creationtime))/60.0))::numeric, 1) "
        "AS p90_wait_min "
        "FROM doma_panda.jobsarchived4 "
        "WHERE creationtime > now() - interval '%s days' "
        "AND starttime IS NOT NULL AND starttime > creationtime "
        "AND jobstatus IN ('finished','failed') "
        "GROUP BY computingsite HAVING count(*) > 50 "
        "ORDER BY median_wait_min" % WEATHER_WINDOW_DAYS)
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cache.set(WEATHER_CACHE_KEY, rows, WEATHER_CACHE_TTL)
    return rows


def analysis_view(request):
    queues, activity, weather = [], [], []
    error = ''
    try:
        queues = _analysis_queues()
        activity = _analysis_activity()
        weather = _analysis_weather()
    except Exception as exc:
        error = str(exc)
    return render(request, 'monitor_app/analysis.html', {
        'queues': queues,
        'activity': activity,
        'weather': weather,
        'weather_window_days': WEATHER_WINDOW_DAYS,
        'error': error,
    })
