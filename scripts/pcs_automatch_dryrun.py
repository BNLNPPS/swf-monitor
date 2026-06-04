#!/usr/bin/env python3
"""
pcs_automatch_dryrun.py — read-only preview of the physics-tag automatch.

The catalog import pins every legacy ``csv_import`` row to one placeholder
physics tag; the automatch (``derive_physics`` + ``find_or_create_physics_tag``,
wired into ``import_default_datasets_csv``) derives each row's real physics from
its EVGEN path and rebinds it to the matching locked tag, creating or locking
tags as needed. This script runs that derivation + matching against the live
catalog **without writing anything** (``dry_run=True`` throughout, no save) and
reports what a reload would do: tags reused, drafts that would be locked in
place, distinct new tags created, rows rebound, backgrounds parked, and any
unrecognized path.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/pcs_automatch_dryrun.py
"""
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from pcs.models import Dataset, PhysicsTag  # noqa: E402
from pcs.physics_match import derive_physics  # noqa: E402
from pcs.services import (  # noqa: E402
    find_or_create_physics_tag,
    _task_name_from_path,
    _extract_csv_filters,
)


def _param_key(derived):
    """Identity of a would-be tag, for deduping creates into distinct tags."""
    if derived.get('process') == 'SINGLE':
        return ('SINGLE', derived.get('particle', ''), derived.get('gun_energy', ''))
    return (derived.get('process'),
            derived.get('beam_energy_electron'),
            derived.get('beam_energy_hadron'))


def main():
    anchor = PhysicsTag.objects.filter(status='locked').order_by('tag_number').first()
    rows = Dataset.objects.filter(dataset_name__startswith='csv_import.').order_by('id')

    reuse_locked = set()        # distinct tag labels reused as-is (already locked)
    lock_in_place = set()       # distinct draft tag labels that would be locked
    new_tags = set()            # distinct param-keys with no existing match -> create
    rebound = 0                 # rows that would move off the placeholder anchor
    reuse_rows = 0              # rows that bind to an existing (reused) tag
    new_rows = 0                # rows that bind to a newly-created tag
    parked = 0                  # background rows left on the anchor
    unrecognized = []           # paths the derivation cannot parse

    for ds in rows:
        ds_path = ds.get_metadata_value('source', 'location', default='') or ''
        task_name = _task_name_from_path(ds_path) or ds.dataset_name
        filters = _extract_csv_filters(ds_path, 'epic_craterlake')
        derived = derive_physics(task_name, beam=filters.get('beam', ''))
        if derived is None:
            unrecognized.append(task_name)
            continue
        if derived.get('process') in ('BEAMGAS', 'SYNRAD'):
            parked += 1
            continue
        tag, action = find_or_create_physics_tag(derived, dry_run=True)
        rebound += 1
        if action == 'reuse-locked':
            reuse_locked.add(tag.tag_label)
            reuse_rows += 1
        elif action == 'lock-in-place':
            lock_in_place.add(tag.tag_label)
            reuse_rows += 1
        else:  # create
            new_tags.add(_param_key(derived))
            new_rows += 1

    print('PCS physics-tag automatch — DRY RUN (no writes)')
    print(f'  placeholder anchor tag: {anchor.tag_label if anchor else "(none)"}')
    print(f'  csv_import rows:        {rows.count()}')
    print(f'  rows rebound:           {rebound}  ({reuse_rows} reuse existing, {new_rows} bind new)')
    print(f'  backgrounds parked:     {parked}')
    print(f'  tags reused (locked):   {len(reuse_locked)}')
    print(f'  draft tags locked in place: {len(lock_in_place)}')
    print(f'  distinct new tags:      {len(new_tags)}')
    print(f'  reused + new total:     {len(reuse_locked) + len(lock_in_place) + len(new_tags)}')
    if lock_in_place:
        print('  would lock in place: ' + ', '.join(sorted(lock_in_place)))
    if unrecognized:
        print(f'  UNRECOGNIZED paths ({len(unrecognized)}):')
        for p in unrecognized:
            print(f'    - {p}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
