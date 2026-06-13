#!/usr/bin/env python3
"""
pcs_physics_completion_dryrun.py — read-only preview of the physics-tag re-tag.

Runs the real derivation + matching (``physics_match.derive_physics`` +
``services.find_or_create_physics_tag(dry_run=True)``) over both catalog
populations — the csv_import EVGEN paths and the 4900 past Rucio DIDs — and
reports what a catalog reload WOULD do once the completed-physics code is
deployed: rows resolved, existing tags reused, distinct new tags created (per
category), backgrounds routed to the signal-free p6001 tag, the p1006 anchor
unwind, and any path whose physics cannot be derived. Writes nothing.

Usage::
    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/pcs_physics_completion_dryrun.py
"""
import os
import sys
from collections import Counter, defaultdict

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
import django  # noqa: E402
django.setup()
from pcs.models import Dataset  # noqa: E402
from pcs.physics_match import derive_physics  # noqa: E402
from pcs.services import (  # noqa: E402
    find_or_create_physics_tag, _task_name_from_path, _extract_csv_filters,
    _physics_key, _PROCESS_CATEGORY,
)

CAT_NAME = {1: 'Single', 2: 'DIS', 3: 'DVCS', 4: 'SIDIS', 5: 'Exclusive', 6: 'Background'}


def _rows():
    """Yield (population, current_tag_label, derived) for every catalog row."""
    for ds in Dataset.objects.filter(dataset_name__startswith='csv_import.') \
            .select_related('physics_tag').iterator():
        loc = ds.get_metadata_value('source', 'location', default='') or ''
        task_name = _task_name_from_path(loc) or ds.dataset_name
        beam = _extract_csv_filters(loc, 'epic_craterlake').get('beam', '')
        yield 'csv_import', ds.physics_tag.tag_label, task_name, derive_physics(task_name, beam=beam)
    for ds in Dataset.objects.filter(dataset_name__startswith='past.') \
            .select_related('physics_tag').iterator():
        po = ds.get_metadata_value('past_output', default={}) or {}
        remainder = (po.get('path') or {}).get('path_remainder', '')
        beam = (po.get('filters') or {}).get('beam', '')
        yield 'past', ds.physics_tag.tag_label, remainder, derive_physics(remainder, beam=beam)


def main():
    n = Counter()
    reused = set()                      # existing tag labels reused
    new_keys = defaultdict(set)         # category -> set of new param-keys
    reuse_by_cat = defaultdict(set)     # category -> reused tag labels
    current = Counter()                 # current physics_tag of scanned rows
    moved_off_anchor = 0
    unrecognised = []

    for pop, cur_label, path, derived in _rows():
        n[pop] += 1
        current[cur_label] += 1
        if derived is None:
            unrecognised.append(path)
            continue
        proc = derived.get('process')
        if proc in ('BEAMGAS', 'SYNRAD'):
            n['background_p6001'] += 1
            continue
        tag, action = find_or_create_physics_tag(derived, dry_run=True)
        cat = _PROCESS_CATEGORY.get(proc)
        if action == 'reuse':
            reused.add(tag.tag_label)
            reuse_by_cat[cat].add(tag.tag_label)
            if tag.tag_label != cur_label:
                moved_off_anchor += 1
        else:
            new_keys[cat].add(_physics_key(derived))
            moved_off_anchor += 1            # a brand-new tag is always a move

    anchor_label, anchor_n = current.most_common(1)[0]
    print('=' * 70)
    print('PCS PHYSICS-TAG RE-TAG — DRY RUN (no writes)')
    print('=' * 70)
    print(f'rows scanned: csv_import={n["csv_import"]}  past={n["past"]}')
    print(f'  resolved to a physics tag, moved off current binding: {moved_off_anchor}')
    print(f'  backgrounds -> p6001 (signal-free): {n["background_p6001"]}')
    print(f'  unrecognised (kept on anchor): {len(unrecognised)}')
    print(f'\ncurrent dominant binding (the anchor): {anchor_label} holds {anchor_n} rows')

    print('\nresulting physics tags per category (reuse existing + create new):')
    total_reuse = total_new = 0
    for cat in sorted(set(reuse_by_cat) | set(new_keys)):
        r, c = len(reuse_by_cat.get(cat, ())), len(new_keys.get(cat, ()))
        total_reuse += r
        total_new += c
        print(f'  cat {cat} {CAT_NAME.get(cat, "?"):10s} reuse {r:3d}  create {c:3d}  total {r + c:3d}')
    print(f'  {"":15s} reuse {total_reuse:3d}  create {total_new:3d}  '
          f'TOTAL {total_reuse + total_new:3d} distinct physics tags')

    if unrecognised:
        u = Counter(unrecognised)
        print(f'\nUNRECOGNISED paths ({len(unrecognised)} rows, {len(u)} distinct):')
        for p, c in u.most_common(20):
            print(f'  {c:4d}x  {p!r}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
