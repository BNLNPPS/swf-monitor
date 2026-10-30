#!/usr/bin/env python3
"""Create What's new items from a reviewed YAML ingest file.

Each record carries the item fields of docs/WHATS_NEW.md (The item):
title, summary, details, refs (a list, written as Markdown lines), tags,
importance, shipped, contact and builds_on. Other keys, such as the
evidence a record carries for review, are not item fields and are not
written. Every item is created through help/whats_new.py item_create, the
call the New item form makes, as the person who approved the ingest. An
item whose slug exists is left as it is. An item that builds on another
record of the file is created after it.

By hand, on swf-testbed:

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/whats-new-ingest.py \\
        ../scripts/whats-new-backfill-2026-10-30.yaml --as wenaus [--dry-run]
"""
import argparse
import json
import logging
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

import yaml  # noqa: E402

from help import whats_new as wn  # noqa: E402
from monitor_app.epicprod_logging import log_epicprod_action  # noqa: E402

log = logging.getLogger('whats-new-ingest')


def _fields(record):
    """The item fields of one ingest record."""
    fields = {k: record[k] for k in wn.FIELDS if record.get(k) is not None}
    refs = fields.get('refs')
    if isinstance(refs, list):
        fields['refs'] = '\n'.join(f'- {r}' for r in refs)
    if fields.get('shipped') is not None:
        fields['shipped'] = str(fields['shipped'])
    return fields


def _ordered(records):
    """Records in an order where every builds_on target in the file comes
    first; the file's own order otherwise, oldest last becomes oldest first."""
    pending = list(reversed(records))
    slugs = {wn.slug_for(r.get('title', '')) for r in records}
    done, out = set(), []
    while pending:
        ready = [r for r in pending
                 if all(s in done or s not in slugs
                        for s in (r.get('builds_on') or []))]
        if not ready:
            raise ValueError('builds_on links in the file form a cycle: '
                             + ', '.join(r.get('title', '') for r in pending))
        for r in ready:
            pending.remove(r)
            done.add(wn.slug_for(r.get('title', '')))
            out.append(r)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('path', help='the YAML ingest file, a list of records')
    parser.add_argument('--as', dest='person', required=True,
                        help='the account of the person who approved the ingest')
    parser.add_argument('--dry-run', action='store_true',
                        help='validate and print what would be created; write nothing')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format='%(levelname)s %(message)s')

    created, present, errors = [], [], []
    try:
        with open(args.path) as fh:
            records = yaml.safe_load(fh)
        if not isinstance(records, list):
            raise ValueError(f'{args.path}: expected a list of records')
        batch = {wn.slug_for(r.get('title', '')) for r in records}
        for record in _ordered(records):
            fields = _fields(record)
            slug = wn.slug_for(fields.get('title', ''))
            if wn.get_entry(slug) is not None:
                log.info('present, left as it is: %s', slug)
                present.append(slug)
                continue
            try:
                if args.dry_run:
                    check = dict(fields, builds_on=[
                        s for s in fields.get('builds_on') or [] if s not in batch])
                    wn.validate_new(check, changed_by=args.person)
                    log.info('would create: %s (%s)', slug, fields.get('shipped'))
                else:
                    wn.item_create(fields, changed_by=args.person)
                    log.info('created: %s (%s)', slug, fields.get('shipped'))
                created.append(slug)
            except wn.WhatsNewError as exc:
                log.error('refused: %s: %s', slug, exc.detail)
                errors.append({'slug': slug, 'error': exc.detail})
    except (OSError, ValueError, yaml.YAMLError) as exc:
        log.error('%s', exc)
        errors.append({'slug': '', 'error': str(exc)})

    summary = {'dry_run': args.dry_run, 'created': len(created),
               'present': len(present), 'errors': errors}
    if not args.dry_run and (created or errors):
        log_epicprod_action(
            'script', 'whats_new_ingest',
            outcome='error' if errors else 'ok', sublevel='normal',
            live_default=False, username=args.person,
            level=logging.ERROR if errors else logging.INFO,
            message=(f"What's new ingest from {os.path.basename(args.path)}: "
                     f'{len(created)} created, {len(present)} present, '
                     f'{len(errors)} refused'))
    print(json.dumps(summary))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
