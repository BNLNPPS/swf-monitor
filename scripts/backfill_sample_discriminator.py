#!/usr/bin/env python3
"""
backfill_sample_discriminator.py — one-shot identity backfill.

Make the composed dataset name (``Dataset.build_dataset_name``) a unique
identity for every live dataset, by materializing the two discriminators that
the tag run alone does not carry (PCS.md §Sample Variants, JEDI_INTEGRATION
§Output naming). Two independent corrections:

  1. Radiative corrections -> evgen tag. A generator whose EVGEN paths carry
     both ``Rad`` and ``noRad`` conflates two distinct physics configurations
     under one evgen tag. Split it: the existing tag becomes the noRad tag
     (``radiative='off'``), a new tag carries ``radiative='on'``, and the Rad
     datasets rebind to it. Generators seen with only one radiative state just
     get the matching ``radiative`` value recorded (no new tag, no rebind).

  2. Single-particle angle -> ``Dataset.sample_name``. SINGLE samples share a
     ``(particle, energy)`` physics tag and differ only by polar-angle range —
     a production discriminator, not a tag. Set ``sample_name`` from the path
     tail (``single_particle_angle``), so it composes after the tag run.

Scope: live datasets only — those referenced by a non-``past_output`` ProdTask.
Idempotent: re-running makes no further change once applied.

Default is a DRY RUN: the work is performed inside a transaction that is then
rolled back, so the before/after composed-name collision counts it prints are
real, not predicted. Pass ``--apply`` to commit.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/backfill_sample_discriminator.py            # dry-run
    python ../scripts/backfill_sample_discriminator.py --apply    # persist

One-shot intra-app backfill, not an operational tool, so it boots Django
directly rather than going through REST. After running once on a deployment it
can be archived.
"""
import argparse
import os
import sys
from pathlib import Path


class _Rollback(Exception):
    """Sentinel used to roll back the dry-run transaction after measuring."""


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument('--apply', action='store_true',
                    help='Persist changes (default: dry-run, rolled back)')
    args = ap.parse_args()

    src = Path(__file__).resolve().parent.parent / 'src'
    sys.path.insert(0, str(src))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE',
                          'swf_monitor_project.settings')
    import django
    django.setup()

    from collections import defaultdict
    from django.db import transaction
    from pcs.models import Dataset, ProdTask
    from pcs.physics_match import derive_evgen, single_particle_angle
    from pcs.services import _task_name_from_path, find_or_create_evgen_tag

    live_ids = set(
        ProdTask.objects.exclude(status='past_output')
        .values_list('dataset_id', flat=True)
    )
    live = list(
        Dataset.objects.filter(id__in=live_ids).select_related(
            'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag')
    )
    print(f'Live datasets (referenced by non-past_output tasks): {len(live)}')

    def collisions(datasets):
        by_name = defaultdict(list)
        for d in datasets:
            by_name[d.build_dataset_name()].append(d.id)
        return {n: ids for n, ids in by_name.items() if len(ids) > 1}

    before = collisions(live)
    print(f'Composed-name collisions BEFORE: {len(before)} names over '
          f'{sum(len(v) for v in before.values())} datasets')
    print()

    def relpath(d):
        return _task_name_from_path(d.source_location)

    def rad_state(d):
        segs = relpath(d).split('/')
        if 'noRad' in segs:
            return 'off'
        if 'Rad' in segs:
            return 'on'
        return ''

    changes = {'tag_radiative_set': 0, 'rad_tags_created': 0,
               'datasets_rebound': 0, 'sample_name_set': 0, 'warnings': []}

    try:
        with transaction.atomic():
            # ---- 1. Radiative corrections -> evgen tag ----
            by_tag = defaultdict(list)
            for d in live:
                if rad_state(d):
                    by_tag[d.evgen_tag_id].append(d)

            for tag_id, ds_list in by_tag.items():
                tag = ds_list[0].evgen_tag
                # A tag that also carries datasets with no radiative path
                # segment can't be cleanly labelled — flag, don't guess.
                others = [d for d in live
                          if d.evgen_tag_id == tag_id and not rad_state(d)]
                if others:
                    msg = (f'evgen {tag.tag_label} mixes radiative and '
                           f'non-radiative datasets — left unsplit')
                    changes['warnings'].append(msg)
                    print(f'  WARNING: {msg}')
                    continue

                states = sorted({rad_state(d) for d in ds_list})
                if states in (['off'], ['on']):
                    v = states[0]
                    params = dict(tag.parameters or {})
                    if params.get('radiative') != v:
                        params['radiative'] = v
                        tag.parameters = params
                        tag.save(update_fields=['parameters'])
                        changes['tag_radiative_set'] += 1
                        print(f'  set {tag.tag_label} radiative={v} '
                              f'({len(ds_list)} datasets)')
                else:  # both 'off' and 'on' under one tag — split
                    params = dict(tag.parameters or {})
                    if params.get('radiative') != 'off':
                        params['radiative'] = 'off'
                        tag.parameters = params
                        tag.save(update_fields=['parameters'])
                        changes['tag_radiative_set'] += 1
                    on_params = dict(tag.parameters or {})
                    on_params['radiative'] = 'on'
                    on_tag, action = find_or_create_evgen_tag(
                        on_params, created_by='backfill-radiative')
                    if action == 'create':
                        changes['rad_tags_created'] += 1
                    print(f'  split {tag.tag_label} -> noRad stays '
                          f'{tag.tag_label}, Rad -> {on_tag.tag_label} '
                          f'({action})')
                    for d in ds_list:
                        if rad_state(d) == 'on':
                            d.evgen_tag = on_tag
                            d.evgen_tag_id = on_tag.id
                            d.save(update_fields=['evgen_tag'])
                            changes['datasets_rebound'] += 1

            # ---- 2. Single-particle angle -> sample_name ----
            for d in live:
                angle = single_particle_angle(relpath(d))
                if angle and d.sample_name != angle:
                    d.sample_name = angle
                    d.save(update_fields=['sample_name'])
                    changes['sample_name_set'] += 1

            after = collisions(live)
            print()
            print(f"evgen radiative recorded:   {changes['tag_radiative_set']}")
            print(f"new Rad evgen tags created: {changes['rad_tags_created']}")
            print(f"datasets rebound to Rad tag:{changes['datasets_rebound']}")
            print(f"sample_name set (SINGLE):   {changes['sample_name_set']}")
            print()
            print(f'Composed-name collisions AFTER:  {len(after)} names over '
                  f'{sum(len(v) for v in after.values())} datasets')
            if after:
                print('  Residual collisions:')
                for name, ids in after.items():
                    print(f'    {name}  <- datasets {ids}')

            if not args.apply:
                raise _Rollback
    except _Rollback:
        print()
        print('DRY RUN — transaction rolled back, nothing written. '
              'Re-run with --apply to commit.')
        return 0

    print()
    print('APPLIED — changes committed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
