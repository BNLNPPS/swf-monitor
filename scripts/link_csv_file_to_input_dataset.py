#!/usr/bin/env python3
"""
link_csv_file_to_input_dataset.py — one-shot backfill.

Walk ProdTask rows with a non-empty ``csv_file`` and ensure each is linked
to a ``Dataset(stage=evgen, source.kind=csv_manifest, source.location=csv_file)``
via ``overrides['input_dataset_did']``.

Idempotent: skips tasks already linked. Reuses existing input Datasets
when one already records the same source.location.

No DB columns added. ``csv_file`` is preserved as a fallback per
back-compat.

A backfilled input Dataset shares the four tags + detector with the
ProdTask's existing output Dataset, but uses a distinct ``scope``
(default ``<output_scope>.evgen``) so the deterministic DID does not
collide with the output. The convention is local to this backfill —
operators can override with ``--input-scope-suffix``.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/link_csv_file_to_input_dataset.py            # dry-run
    python ../scripts/link_csv_file_to_input_dataset.py --apply    # persist

Note on Django ORM access: this is a one-shot intra-app backfill, not an
operational tool, so it boots Django directly rather than going through
REST. After running once on a deployment it can be archived.
"""
import argparse
import os
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument('--apply', action='store_true',
                    help='Persist changes (default: dry-run)')
    ap.add_argument('--input-scope-suffix', default='evgen',
                    help='Suffix appended to the output Dataset scope to '
                         'form the input Dataset scope (default: evgen)')
    args = ap.parse_args()

    src = Path(__file__).resolve().parent.parent / 'src'
    sys.path.insert(0, str(src))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE',
                          'swf_monitor_project.settings')
    import django
    django.setup()

    from django.db import transaction
    from pcs.models import ProdTask, Dataset

    qs = ProdTask.objects.exclude(csv_file='').order_by('name')
    total = qs.count()
    print(f'ProdTasks with non-empty csv_file: {total}')
    if total == 0:
        print('Nothing to backfill.')
        return 0

    by_csv = {}
    for t in qs:
        by_csv.setdefault(t.csv_file, []).append(t)
    print(f'Unique csv_file values: {len(by_csv)}')
    print()

    plan_create = []   # (csv, sample_task, planned_scope)
    plan_link = []     # (task, csv)
    plan_skip = []     # (task, reason)

    for csv, tasks in by_csv.items():
        sample = tasks[0]
        out_ds = sample.dataset
        existing = Dataset.objects.filter(
            metadata__source__location=csv,
            metadata__source__kind='csv_manifest',
        ).first()
        if existing:
            print(f'  csv={csv!r} → reuse existing Dataset {existing.did}')
        else:
            input_scope = f'{out_ds.scope}.{args.input_scope_suffix}'
            print(f'  csv={csv!r} → CREATE Dataset, scope={input_scope}')
            plan_create.append((csv, sample, input_scope))

        for t in tasks:
            ov = t.overrides or {}
            if ov.get('input_dataset_did') or ov.get('input_dataset_dids'):
                plan_skip.append((t, 'already linked'))
            else:
                plan_link.append((t, csv))

    print()
    print(f'Plan: create={len(plan_create)}  '
          f'link={len(plan_link)}  '
          f'skip={len(plan_skip)}')

    if not args.apply:
        print('\nDry-run only. Re-run with --apply to persist.')
        return 0

    print('\nApplying...')
    with transaction.atomic():
        for csv, sample, input_scope in plan_create:
            out_ds = sample.dataset
            input_ds = Dataset(
                scope=input_scope,
                detector_version=out_ds.detector_version,
                detector_config=out_ds.detector_config,
                physics_tag=out_ds.physics_tag,
                evgen_tag=out_ds.evgen_tag,
                simu_tag=out_ds.simu_tag,
                reco_tag=out_ds.reco_tag,
                description='External EVGEN input (backfilled from csv_file)',
                metadata={
                    'stage': 'evgen',
                    'source': {'kind': 'csv_manifest', 'location': csv},
                },
                created_by='backfill',
            )
            input_ds.save()
            print(f'  CREATED Dataset {input_ds.did}')

        for t, csv in plan_link:
            input_ds = Dataset.objects.filter(
                metadata__source__location=csv,
                metadata__source__kind='csv_manifest',
            ).first()
            if input_ds is None:
                raise RuntimeError(
                    f'Input Dataset for csv={csv!r} not found after create '
                    f'phase — aborting.'
                )
            ov = dict(t.overrides or {})
            ov['input_dataset_did'] = input_ds.did
            t.overrides = ov
            t.save(update_fields=['overrides', 'updated_at'])
            print(f'  LINKED task={t.name} → {input_ds.did}')

    print()
    print(f'Done. created={len(plan_create)}  '
          f'linked={len(plan_link)}  '
          f'skipped={len(plan_skip)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
