#!/usr/bin/env python3
"""
pcs_physics_completion_dryrun.py — read-only enumeration of the FULL physics
signatures the catalog requires, to size "complete the physics in the tags".

Today ``find_or_create_physics_tag`` matches non-single physics on
``(process, beam_e, beam_h)`` only, so Q2/species/decay/charge/beam-config/etc.
collapse onto the first-created tag. This script derives the *full* physics
signature of every catalog row (csv_import EVGEN paths AND past. Rucio DIDs),
dedupes per category, and reports: distinct signatures (= p-tags) per category,
the distinct value set per physics axis (= the schema), the projected max p
index, and any path token it could not classify (so the scanner is refined
against real values, never guesses).

Decisions baked in (Torre, 2026-06-13):
  - angle range is a SAMPLE VARIANT, not a tag -> excluded from the signature.
  - radiation (Rad/noRad) is an EVGEN-tag axis -> excluded from the physics sig.
  - beam_config (hiDiv/hiAcc) is its own axis (split out of the old `model`).
  - EW_BSM/ALP folds into category 5 (Exclusive) as process='ALP', with
    channel + mass as physics axes (mass: one tag per value).
  - generator/version are EVGEN-tag axes -> excluded from the physics sig.

Read-only: no DB writes, no Django objects created.

Usage::
    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/pcs_physics_completion_dryrun.py
"""
import os
import re
import sys
from collections import defaultdict

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
import django  # noqa: E402
django.setup()
from pcs.models import Dataset  # noqa: E402

# ── known physics areas (path roots) and their tag category digit ──────────────
AREA_CATEGORY = {
    'SINGLE': 1, 'DIS': 2, 'DDIS': 2, 'DVCS': 3, 'DDVCS': 3,
    'SIDIS': 4, 'EXCLUSIVE': 5, 'EW_BSM': 5,
}
KNOWN_AREAS = set(AREA_CATEGORY) | {'BACKGROUNDS'}
# EXCLUSIVE subprocess -> its own category digit (DVCS/DDVCS are area 3, rest 5)
EXCL_CATEGORY = {'DVCS': 3, 'DDVCS': 3}

_ABCONV = '_ABCONV'
def _strip_abconv(s):
    return s[:-len(_ABCONV)] if s.endswith(_ABCONV) else s

# ── token recognizers (scanned at ANY position; paths are not positional) ──────
BEAM_RE   = re.compile(r'^\d+x\d+$')
Q2_RE     = re.compile(r'^(minQ2=\d+|q2_\S+)$')
SPECIES_RE= re.compile(r'^e(p|n|D|H[0-9]+|He[0-9]+|Li[0-9]*|Ca|Cu|Ru|Pb|Au|C|O)$')
MASS_RE   = re.compile(r'^ma_[0-9]')
CHANNEL_RE= re.compile(r'^aem')
UPSILON_RE= re.compile(r'^upsilon(1s|2s|3s)(photo|_threshold)_ab_(hiAcc|hiDiv)_(\d+x\d+)')
RADIATION = {'Rad', 'noRad', 'noRC', 'noradcor'}            # EVGEN axis: excluded
BEAMCONFIG= {'hiAcc', 'hiDiv'}
DECAY     = {'edecay', 'mudecay'}
CHARGE    = {'hplus', 'hminus'}
HELICITY  = {'hel_plus', 'hel_minus'}                       # beam helicity state
COHERENCE = {'coherent', 'Coherent'}
MODEL     = {'bsat'}
POLAR     = {'unpolarised', 'polarised'}
FINAL_STATE = {'pi+', 'pi-', 'pi0', 'K+', 'K-', 'K0', 'K+Lambda'}   # exclusive final state
# tokens recognised as generator/version/method noise -> skipped, not physics
GEN_HINT  = re.compile(r'(BeAGLE|EpIC|pythia|sartre|eSTARlight|DEMPgen|lAger|rapgap'
                       r'|DJANGOH|madgraph|MesonSF|HFsim|GETaLM|dijet|PGF|^\d+\.\d+)',
                       re.IGNORECASE)
SKIP_EXACT = {'ab', 'NEW', 'ep_noradcor'}  # afterburner marker / variant noise

def _split_compounds(tok):
    """Yield sub-tokens for compound path segments so embedded physics tokens are
    seen: 'coherent_ep' -> coherent, ep; 'ep_noradcor' -> ep, noradcor."""
    yield tok
    if '_' in tok:
        for part in tok.split('_'):
            yield part


def derive_full_physics(segs):
    """segs starts at the physics AREA. Returns (signature_dict, unrecognised).
    signature_dict is the full set of physics-tag-defining params (the dedup key);
    None for BACKGROUNDS (k-tag / parked, not a physics p-tag)."""
    if not segs:
        return None, []
    area = segs[0]
    if area == 'BACKGROUNDS':
        return None, []
    sig, unrec = {}, []
    rest = segs[1:]

    if area == 'SINGLE':
        return ({'category': 1, 'process': 'SINGLE',
                 'particle': rest[0] if rest else '',
                 'gun_energy': rest[1] if len(rest) > 1 else ''}, [])

    # process / subprocess + category
    if area in ('DIS', 'DDIS'):
        proc = 'DDIS' if area == 'DDIS' else 'DIS'
        if 'NC' in rest: proc = 'DIS_NC'
        elif 'CC' in rest: proc = 'DIS_CC'
        sig['process'], sig['category'] = proc, 2
    elif area == 'SIDIS':
        sub = _strip_abconv(rest[0]) if rest else ''
        sig['process'] = 'SIDIS_' + sub if sub in ('D0', 'DIJET', 'Lc') else 'SIDIS'
        sig['category'] = 4
    elif area == 'EXCLUSIVE':
        sub = _strip_abconv(rest[0]) if rest else ''
        sig['process'] = sub
        sig['category'] = EXCL_CATEGORY.get(sub, 5)
    elif area == 'EW_BSM':
        sig['process'] = rest[0] if rest else ''     # ALP
        sig['category'] = 5
    else:                                            # DVCS/DDVCS as a bare area
        sig['process'] = area
        sig['category'] = AREA_CATEGORY.get(area, 5)

    species = []
    for raw in rest:
        m = UPSILON_RE.match(raw)
        if m:
            sig['state'], sig['mechanism'] = m.group(1), m.group(2).lstrip('_')
            sig['beam_config'], sig['beam'] = m.group(3), m.group(4)
            continue
        classified = False
        for tok in _split_compounds(raw):
            if BEAM_RE.match(tok):       sig['beam'] = tok; classified = True
            elif Q2_RE.match(tok):       sig['q2_range'] = tok; classified = True
            elif SPECIES_RE.match(tok):  species.append(tok); classified = True
            elif tok in BEAMCONFIG:      sig['beam_config'] = tok; classified = True
            elif tok in DECAY:           sig['decay_mode'] = tok; classified = True
            elif tok in CHARGE:          sig['hadron_charge'] = tok; classified = True
            elif tok in HELICITY:        sig['helicity'] = tok; classified = True
            elif tok in COHERENCE:       sig['coherence'] = 'coherent'; classified = True
            elif tok in MODEL:           sig['model'] = tok; classified = True
            elif tok in POLAR:           sig['polarization'] = tok; classified = True
            elif tok in FINAL_STATE:     sig['final_state'] = tok; classified = True
            elif MASS_RE.match(tok):     sig['mass'] = tok; classified = True
            elif CHANNEL_RE.match(tok):  sig['channel'] = tok; classified = True
            elif tok in RADIATION:       classified = True       # evgen axis, skip
        # whole-segment noise we intentionally ignore
        if not classified:
            if raw in SKIP_EXACT or GEN_HINT.search(raw) or 'ABCONV' in raw \
               or raw in ('NC', 'CC') or raw == sig.get('process') \
               or raw == _strip_abconv(rest[0] if rest else ''):
                continue
            unrec.append(raw)
    if species:
        sig['species'] = ','.join(species)
    return sig, unrec


def _area_segments(remainder):
    """Drop the leading background-overlay segments (Bkg_*/Synrad_*/GoldC*/<um>/
    Test/...) and return the path from the first known physics area onward."""
    segs = [s for s in (remainder or '').split('/') if s]
    for i, s in enumerate(segs):
        if s in KNOWN_AREAS:
            return segs[i:]
    return []


def main():
    sigs = defaultdict(set)            # category -> set of frozenset(sig.items())
    values = defaultdict(set)          # axis -> set of values
    unrec = defaultdict(int)
    n_rows = {'csv_import': 0, 'past': 0}
    n_skipped_bg = 0
    n_no_area = 0

    rows = Dataset.objects.filter(dataset_name__startswith='csv_import.') \
        | Dataset.objects.filter(dataset_name__startswith='past.')
    for ds in rows.iterator():
        if ds.dataset_name.startswith('csv_import.'):
            loc = ds.get_metadata_value('source', 'location', default='') or ''
            segs = re.sub(r'^.*/EVGEN/', 'EVGEN/', loc).split('/')
            segs = segs[1:] if segs and segs[0] == 'EVGEN' else []
            pop = 'csv_import'
        else:
            rem = ds.get_metadata_value('past_output', 'path', 'path_remainder', default='') or ''
            segs = _area_segments(rem)
            pop = 'past'
        if not segs:
            n_no_area += 1
            continue
        n_rows[pop] += 1
        sig, u = derive_full_physics(segs)
        for tok in u:
            unrec[tok] += 1
        if sig is None:
            n_skipped_bg += 1
            continue
        cat = sig.get('category')
        sigs[cat].add(frozenset((k, v) for k, v in sig.items() if k != 'category'))
        for k, v in sig.items():
            if k not in ('category', 'beam'):
                values[k].add(v)

    print('=' * 72)
    print('PCS PHYSICS-TAG COMPLETION — distinct signatures the catalog requires')
    print('=' * 72)
    print(f'rows scanned: csv_import={n_rows["csv_import"]}  past={n_rows["past"]}'
          f'   (backgrounds skipped={n_skipped_bg}, no-area={n_no_area})')
    cat_name = {1: 'Single', 2: 'DIS', 3: 'DVCS', 4: 'SIDIS', 5: 'Exclusive+EW_BSM'}
    total = 0
    print('\ndistinct physics signatures (= p-tags) per category:')
    for cat in sorted(sigs):
        n = len(sigs[cat])
        total += n
        print(f'  cat {cat} {cat_name.get(cat, "?"):20s} {n:4d}'
              f'   -> p{cat}001..p{cat}{n:03d} band')
    print(f'  {"TOTAL":27s} {total:4d}   (vs 176 today)')
    print(f'  global suffix reaches ~{total}; max label ~= p5{total:03d} '
          f'(highest band = Exclusive, digit 5)')

    print('\nper-axis distinct value sets (= the schema choices):')
    for axis in sorted(values):
        vals = sorted(values[axis])
        shown = vals if len(vals) <= 25 else vals[:25] + [f'...(+{len(vals)-25})']
        print(f'  {axis:14s} ({len(vals):3d}) {shown}')

    if unrec:
        print('\nUNRECOGNISED path tokens (refine the scanner / new axis?):')
        for tok, c in sorted(unrec.items(), key=lambda x: -x[1]):
            print(f'  {c:4d}x  {tok!r}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
