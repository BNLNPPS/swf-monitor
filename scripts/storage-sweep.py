#!/usr/bin/env python3
"""storage-sweep.py — the storage pass: placement state of production
data on every JLab RSE, kept in the storage store and published as the
epicprod ``storage`` Snapper component.

The prod-ops agent's doer for the ``storage_sweep`` step (nightly in
the ``catalog_sync`` chain as a full pass; hourly by cron enqueue as an
incremental pass). Logic in ``swf_epicprod/analytics/storage.py``,
publication in ``monitor_app/snapper_storage.py``; design in
``swf-epicprod/docs/STORAGE.md``. Django-bootstrap standalone script —
also usable by hand.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/storage-sweep.py [--census | --full] [--campaigns 26.07]
                                       [--limit-files N] [--limit-datasets N]
                                       [--resume PASS_ID] [--publish-only]
                                       [--no-publish] [--dump projection.json]

A run with a limit works on a copy of the store and never publishes.
``--publish-only`` skips the crawl and publishes the projection of the
store's last completed pass, for a pass whose publish step failed.
"""
import argparse
import json
import os
import signal
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from swf_epicprod.analytics.storage import (  # noqa: E402
    PassInProgress, log, mark_pass_interrupted, project_store, run_pass)


def _on_signal(signum, frame):
    raise KeyboardInterrupt(signal.Signals(signum).name)


def main():
    # A signal to the doer (a stop by hand, or systemd's stop timeout)
    # ends the pass through the interrupted path below instead of killing
    # the process with its pass row left as if still running.
    signal.signal(signal.SIGTERM, _on_signal)
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--census', action='store_true',
                      help='every file under the production roots, once')
    mode.add_argument('--full', action='store_true',
                      help='every dataset; the target campaigns\' files')
    mode.add_argument('--publish-only', action='store_true',
                      help='no crawl: publish the projection of the store\'s '
                           'last completed pass (after a failed publish)')
    parser.add_argument('--campaigns', default='',
                        help='comma-separated campaign families '
                             '(default: the delivery record\'s targets, or '
                             'every family in a census)')
    parser.add_argument('--limit-files', type=int, default=0,
                        help='cap the file tier for a validation run '
                             '(works on a copy of the store, no publish)')
    parser.add_argument('--limit-datasets', type=int, default=0,
                        help='cap the dataset tier for a validation run')
    parser.add_argument('--resume', type=int, default=0, metavar='PASS_ID',
                        help='continue an interrupted pass by id, skipping '
                             'the locations it already stamped')
    parser.add_argument('--no-publish', action='store_true',
                        help='run the pass without publishing the component')
    parser.add_argument('--dump', default='',
                        help='write the projection JSON to this path')
    parser.add_argument('--created-by', default='prodops_agent')
    args = parser.parse_args()
    mode_name = 'census' if args.census else 'full' if args.full else 'incremental'
    campaigns = tuple(c.strip() for c in args.campaigns.split(',')
                      if c.strip()) or None
    validation = bool(args.limit_files or args.limit_datasets)

    if args.publish_only:
        summary, data = project_store()
    else:
        try:
            summary, data = run_pass(mode_name, campaigns=campaigns,
                                     limit_files=args.limit_files,
                                     limit_datasets=args.limit_datasets,
                                     resume_pass=args.resume or None)
        except PassInProgress as exc:
            # Another pass holds the store, the census or a full pass still
            # running when the hourly enqueue lands: skipped, not failed.
            print('SUMMARY ' + json.dumps({'mode': mode_name,
                                           'skipped': str(exc)}))
            return 4
        except KeyboardInterrupt as exc:
            # The pass row records the interruption and stays unfinished:
            # the next pass redoes the interval and is not blocked once
            # this process is gone (storage.py, run_pass).
            pass_id = mark_pass_interrupted(str(exc) or 'interrupt')
            log(f'pass {pass_id} interrupted: {exc}')
            print('SUMMARY ' + json.dumps({'mode': mode_name,
                                           'pass_id': pass_id,
                                           'interrupted': str(exc)}))
            return 143
    if args.dump:
        with open(args.dump, 'w') as handle:
            json.dump(data, handle, indent=1, sort_keys=True)
        print(f'projection written to {args.dump}')

    if validation or args.no_publish:
        summary['published'] = False
    else:
        from monitor_app.snapper_storage import publish_storage
        publication = publish_storage(data, since=data['interval']['start'])
        summary['published'] = True
        summary['revision'] = publication.update.revision
        summary['content_changed'] = publication.update.content_changed
        # The ghost product is rebuilt as the pass's last step, so the
        # Storage exceptions page, the epicprod_storage tool and the REST
        # listing serve this pass's ghosts at once (STORAGE.md, Retrieval;
        # swf-monitor docs/CACHED_PRODUCTS.md). A failure here is logged
        # and never fails the sweep: the product's TTL rebuilds it behind
        # the next page view.
        try:
            from swf_epicprod.analytics.storage_listings import (
                refresh_ghost_product)
            summary['ghost_product'] = refresh_ghost_product()
        except Exception as exc:                                  # noqa: BLE001
            log(f'ERROR ghost product refresh: {exc}')
            summary['ghost_product'] = {'error': str(exc)}
    print('SUMMARY ' + json.dumps(summary))
    return 0 if not summary.get('errors') else 3


if __name__ == '__main__':
    sys.exit(main())
