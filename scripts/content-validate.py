#!/usr/bin/env python3
"""content-validate.py — reconcile a sample's dataset against the record.

The production-ops agent's doer for content validation (swf-epicprod
docs/EPICPROD_VALIDATION.md, Content validation). The record states which
file is the delivered output of each work unit and how many events it
carries; the Rucio dataset states what it holds. This reads both and
names what would make an availability signal false.

It proposes and never acts: nothing is detached, nothing is written to
Rucio. Acceptance is an operator's single action, and acceptance never
deletes.

Reading JLab Rucio is a remote call, so it belongs here rather than in a
page. The finding is stored as a cached product, `content_validation:<task>`,
which is what pages read.

Django-bootstrap standalone script — also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/content-validate.py \
        [--task <composed name>] [--hours 168] [--json]

The last stdout line is a JSON summary; findings go to stderr readably.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone as dt_timezone

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

DEFAULT_HOURS = 168


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def tasks_to_check(since, name=''):
    """Tasks whose record has moved: those are the ones worth reconciling."""
    from pcs.models import DeliveredOutput, ProdTask
    if name:
        task = ProdTask.objects.filter(name=name).first() or \
            ProdTask.objects.filter(dataset__composed_name=name).first()
        return [task] if task else []
    ids = (DeliveredOutput.objects
           .filter(updated_at__gte=since)
           .values_list('prod_task_id', flat=True).distinct())
    return list(ProdTask.objects.filter(id__in=list(ids)))


def store(task, findings):
    """Keep the finding where pages can read it without a remote call.

    The house cached-product store, written by a forced rebuild: this doer
    is the builder, and a page is only ever a reader (docs/CACHED_PRODUCTS.md).
    """
    from monitor_app.cached_product import get_product
    key = f'content_validation:{task.composed_name or task.name}'
    payload = {'task': task.composed_name or task.name, 'findings': findings,
               'built_at': datetime.now(dt_timezone.utc).isoformat()}
    get_product(key, lambda: payload, ttl_seconds=24 * 3600, refresh=True)
    return key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', default='', help='one task by composed name')
    parser.add_argument('--hours', type=float, default=DEFAULT_HOURS,
                        help='reconcile tasks whose record moved in this window')
    parser.add_argument('--json', action='store_true',
                        help='print every finding as JSON')
    args = parser.parse_args()

    from pcs import content_validation

    since = datetime.now(dt_timezone.utc) - timedelta(hours=args.hours)
    summary = {'tasks': 0, 'datasets': 0, 'sound': 0, 'unsound': 0,
               'unreadable': 0, 'delivered_events': 0}

    for task in tasks_to_check(since, name=args.task):
        findings = content_validation.reconcile_all(task)
        if not findings:
            continue
        summary['tasks'] += 1
        for finding in findings:
            summary['datasets'] += 1
            if finding['error']:
                summary['unreadable'] += 1
            elif finding['sound']:
                summary['sound'] += 1
            else:
                summary['unsound'] += 1
            summary['delivered_events'] += finding['delivered_events']
            _log(f"{finding['task']} {finding['dataset']}: "
                 f"units={finding['units']} matched={len(finding['matched'])} "
                 f"events={finding['delivered_events']} "
                 f"orphans={len(finding['orphan_files'])} "
                 f"missing={len(finding['units_without_file'])} "
                 f"several={len(finding['units_with_several'])} "
                 f"no_count={len(finding['files_without_events'])} "
                 f"{'sound' if finding['sound'] else 'NOT SOUND'}"
                 f"{' — ' + finding['error'] if finding['error'] else ''}")
            if args.json:
                _log(json.dumps(finding, indent=2, default=str))
        try:
            store(task, findings)
        except Exception as e:                                # noqa: BLE001
            _log(f'ERROR: finding not stored for {task}: {e}')

    print(json.dumps(summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
