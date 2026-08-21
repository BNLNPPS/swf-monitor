"""Remove the superseded counter-form errors component from snap history.

The errors component's first day recorded running counters (version 1);
the interval-entries record (version 2, docs/SNAPPER_ERRORS.md) and its
backfill supersede that span completely. This script removes the
counter-form errors member from epicprod snap history:

- A snap whose only changed component was errors is deleted — its
  other-component state duplicates the preceding snap.
- A snap that also carried other components' changes (or a baseline
  copy) keeps its row; the errors member is stripped from the state
  and the envelope vectors, and the composed state hash is cleared as
  no longer computed.

Rows written by the entries backfill (capture policy
backfill-errors-v1) and version-2 entries snaps are never touched. A
row referenced by a capture cursor is stripped rather than deleted.
Idempotent; dry-run default.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-monitor>/scripts/cleanup-errors-counter-era.py [--apply]
"""

import argparse
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from snapper_ai.models import CaptureCursor, SystemSnap  # noqa: E402

BACKFILL_POLICY = 'backfill-errors-v1'


def main():
    parser = argparse.ArgumentParser(
        description='Remove the counter-form errors component from '
                    'epicprod snap history.')
    parser.add_argument('--apply', action='store_true',
                        help='write the changes (dry run without)')
    args = parser.parse_args()

    protected = set(
        CaptureCursor.objects
        .filter(latest_snap__isnull=False)
        .values_list('latest_snap_id', flat=True))

    rows = (SystemSnap.objects
            .filter(scope='epicprod',
                    state__components__has_key='errors')
            .exclude(capture_policy=BACKFILL_POLICY)
            .order_by('snap_time'))

    deletions = []
    strips = []
    for snap in rows.iterator():
        errors = (snap.state.get('components') or {}).get('errors') or {}
        if 'entries' in (errors.get('data') or {}):
            continue
        changed = list(snap.changed_components or [])
        if changed == ['errors'] and snap.id not in protected:
            deletions.append(snap)
        else:
            strips.append(snap)

    print(f'counter-form errors members found: '
          f'{len(deletions) + len(strips)}')
    print(f'  rows to delete (errors-only change): {len(deletions)}')
    print(f'  rows to strip (other components kept): {len(strips)}')
    for snap in (deletions + strips)[:3]:
        print(f'  e.g. {snap.snap_time.isoformat()} '
              f'changed={snap.changed_components}')

    if not args.apply:
        print('\ndry run — nothing written; --apply performs the cleanup')
        return 0

    deleted = 0
    for snap in deletions:
        snap.delete()
        deleted += 1
    stripped = 0
    for snap in strips:
        snap.state.get('components', {}).pop('errors', None)
        snap.changed_components = [
            name for name in (snap.changed_components or [])
            if name != 'errors']
        for field in ('component_revisions', 'registration_versions',
                      'component_hashes'):
            mapping = getattr(snap, field) or {}
            mapping.pop('errors', None)
            setattr(snap, field, mapping)
        snap.state_hash = ''
        snap.save(update_fields=[
            'state', 'changed_components', 'component_revisions',
            'registration_versions', 'component_hashes', 'state_hash'])
        stripped += 1
    print(f'\napplied: deleted {deleted}, stripped {stripped}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
