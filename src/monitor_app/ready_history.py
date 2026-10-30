"""Daily ready-task depth from the production front's recorded cycles."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .models import AppLog

DECISION_MAX_AGE = timedelta(minutes=75)  # Hourly decision heartbeat plus one cycle's grace.
CYCLE_MAX_AGE = timedelta(minutes=15)


def ready_samples(events):
    """Join each cycle to its current queues' most recent depth readings.

    Decisions are stored on changes or at least hourly. The cycle records
    the number of regulated queues; its newest fresh queue readings avoid
    carrying retired queue state through later queue-registry changes.
    """
    latest = {}
    for stamp, action, queue, depth, queue_count, outcome in events:
        if action == 'front_decision':
            if queue and isinstance(depth, int) and depth >= 0:
                latest[queue] = (stamp, depth)
            continue
        fresh = sorted((point for point in latest.values()
                        if stamp - point[0] <= DECISION_MAX_AGE), reverse=True)
        complete = (outcome == 'ok' and isinstance(queue_count, int)
                    and queue_count >= 0 and len(fresh) >= queue_count)
        yield stamp, sum(depth for _, depth in fresh[:queue_count]) if complete else None


def daily_means(samples, start, end):
    """Time-weight samples over observed time, retaining gaps and coverage."""
    area, covered = defaultdict(float), defaultdict(float)
    for index, (stamp, count) in enumerate(samples):
        if count is None:
            continue
        begin = max(start, stamp)
        stop = min(end, stamp + CYCLE_MAX_AGE,
                   samples[index + 1][0] if index + 1 < len(samples) else end)
        while begin < stop:
            midnight = datetime.combine(begin.date() + timedelta(days=1), datetime.min.time(),
                                        tzinfo=timezone.utc)
            boundary = min(stop, midnight)
            seconds = (boundary - begin).total_seconds()
            key = str(begin.date())
            area[key] += count * seconds
            covered[key] += seconds
            begin = boundary
    return {day: {'ready_tasks': area[day] / seconds,
                  'ready_coverage_hours': seconds / 3600}
            for day, seconds in covered.items() if seconds > 0}


def history(start, end):
    """One bounded action-stream read; no individual job or task queries."""
    begin = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    stop = datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc)
    rows = (AppLog.objects.filter(
        app_name='epicprod', timestamp__gte=begin - timedelta(hours=2), timestamp__lt=stop,
        extra_data__action__in=('front_decision', 'front_cycle'))
        .order_by('timestamp', 'id').values_list(
            'timestamp', 'extra_data__action', 'extra_data__subject_key',
            'extra_data__ready_total', 'extra_data__queues', 'extra_data__outcome'))
    return daily_means(list(ready_samples(rows.iterator())), begin, stop)
