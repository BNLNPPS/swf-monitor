"""Derive canonical physics-tag parameters from a catalog task's EVGEN path.

The legacy ``csv_import`` catalog encodes a task's real physics in its path
(``EVGEN/<category>/<sub>/.../<beam>/...``), not in a physics tag — the import
pinned every row to one placeholder anchor tag, so the bound physics (beam,
process) is wrong for almost all of them. This module parses the path into the
parameters a physics tag carries, so each task can be matched to the correct
existing tag, or have the right tag created. See EPICPROD_TASK_CATALOG.md.

The single source of truth is the path. ``derive_physics(path)`` returns the
canonical params; the caller matches/creates the PhysicsTag and rebinds.
"""
import re


#: EXCLUSIVE / SIDIS sub-process folders carry this afterburner-conversion suffix.
_ABCONV = '_ABCONV'


def _strip_abconv(s):
    return s[:-len(_ABCONV)] if s.endswith(_ABCONV) else s


def _beam_pair(beam):
    """'18x275' -> ('18', '275'); '' -> ('N/A', 'N/A')."""
    if beam and 'x' in beam:
        e, h = beam.split('x', 1)
        return (e or 'N/A', h or 'N/A')
    return ('N/A', 'N/A')


def derive_physics(path, beam=''):
    """Canonical physics-tag params from a stripped EVGEN path (the task name).

    ``path`` is the ``/volatile/eic/EPIC/`` -stripped path, i.e. ``EVGEN/<cat>/…``.
    ``beam`` is the parsed beam (``overrides['csv_import']['filters']['beam']``),
    used because it is already extracted; the path is otherwise authoritative.

    Returns a dict of physics-tag parameters, or ``None`` for a path that is not
    a recognizable EVGEN catalog entry. Angular range (single-particle) is NOT
    included — it is a per-task override, not part of the reusable tag.
    """
    segs = (path or '').split('/')
    if len(segs) < 2 or segs[0] != 'EVGEN':
        return None
    cat = segs[1]
    e, h = _beam_pair(beam)
    base = {'beam_energy_electron': e, 'beam_energy_hadron': h}

    if cat == 'SINGLE':
        # EVGEN/SINGLE/<particle>/<energy>/<angle...>
        return {
            'process': 'SINGLE',
            'beam_energy_electron': 'N/A', 'beam_energy_hadron': 'N/A',
            'particle': segs[2] if len(segs) > 2 else '',
            'gun_energy': segs[3] if len(segs) > 3 else '',
        }

    if cat == 'BACKGROUNDS':
        sub = segs[2] if len(segs) > 2 else ''
        if sub == 'SYNRAD':
            return {**base, 'process': 'SYNRAD'}
        # BEAMGAS: EVGEN/BACKGROUNDS/BEAMGAS/<source>/<mechanism-or-generator>/...
        return {
            **base, 'process': 'BEAMGAS',
            'bg_source': segs[3] if len(segs) > 3 else '',
            'bg_mechanism': segs[4] if len(segs) > 4 else '',
        }

    if cat == 'EXCLUSIVE':
        return {**base, 'process': _strip_abconv(segs[2]) if len(segs) > 2 else 'EXCLUSIVE'}

    if cat == 'SIDIS':
        sub = segs[2] if len(segs) > 2 else ''
        if sub.endswith(_ABCONV):
            return {**base, 'process': 'SIDIS_' + _strip_abconv(sub)}
        return {**base, 'process': 'SIDIS'}     # sub is a generator folder

    if cat == 'DIS':
        sub = segs[2] if len(segs) > 2 else ''
        if sub in ('NC', 'CC'):
            return {**base, 'process': 'DIS_' + sub}
        return {**base, 'process': 'DIS'}        # sub is a generator folder

    if cat == 'DDIS':
        return {**base, 'process': 'DDIS'}

    return {**base, 'process': cat}              # unknown category — surfaced as-is


#: BEAMGAS 4th path segment is a physical mechanism when it is one of these;
#: otherwise it names the generator.
_BG_MECHANISMS = ('brems', 'coulomb', 'touschek')
_BG_BEAM_NXM = re.compile(r'^(\d+)x(\d+)$')
_BG_BEAM_SINGLE = re.compile(r'^(\d+)GeV$')


def _bg_beam(segs, source):
    """Beam energies from a backgrounds path. 'NxM' -> (N, M). A bare 'NGeV' is
    assigned by source: a proton source to the hadron beam, otherwise electron."""
    for s in segs:
        m = _BG_BEAM_NXM.match(s)
        if m:
            return m.group(1), m.group(2)
        m = _BG_BEAM_SINGLE.match(s)
        if m:
            return ('N/A', m.group(1)) if source == 'proton' else (m.group(1), 'N/A')
    return 'N/A', 'N/A'


def derive_background(path):
    """Canonical background (k) tag params from a stripped EVGEN/BACKGROUNDS path,
    or None if the path is not a backgrounds entry.

    BEAMGAS: ``EVGEN/BACKGROUNDS/BEAMGAS/<source>/<mechanism-or-generator>/…/<beam>/…``.
    The 4th segment is a physical mechanism (brems/coulomb/touschek) when it is
    one, otherwise it is the generator. SYNRAD has no source or mechanism. All
    values are open strings — the parser passes through whatever the path names.
    Always returns the full set of match fields (blank where not present) so the
    tag-dedup lookup is consistent.
    """
    segs = (path or '').split('/')
    if len(segs) < 3 or segs[0] != 'EVGEN' or segs[1] != 'BACKGROUNDS':
        return None
    sub = segs[2]
    params = {
        'background_type': sub,
        'bg_source': '', 'bg_mechanism': '', 'bg_generator': '',
        'beam_energy_electron': 'N/A', 'beam_energy_hadron': 'N/A',
    }
    if sub == 'SYNRAD':
        if len(segs) > 3:
            params['bg_generator'] = segs[3]
    else:
        source = segs[3] if len(segs) > 3 else ''
        params['bg_source'] = source
        seg4 = segs[4] if len(segs) > 4 else ''
        if seg4 in _BG_MECHANISMS:
            params['bg_mechanism'] = seg4
            gen_parts = [s for s in segs[5:7]
                         if s and not _BG_BEAM_NXM.match(s) and not _BG_BEAM_SINGLE.match(s)]
            params['bg_generator'] = '/'.join(gen_parts)
        else:
            params['bg_generator'] = seg4
    e, h = _bg_beam(segs, params['bg_source'])
    params['beam_energy_electron'] = e
    params['beam_energy_hadron'] = h
    return params


#: Generator family names whose version follows the name in a token. pythia is
#: handled separately because its family carries the major-version digit
#: (pythia8 / pythia6).
_EVGEN_NAMES = ('EpIC', 'BeAGLE', 'sartre', 'eSTARlight', 'eicMesonSFGen',
                'lAger', 'rapgap', 'DEMPgen', 'DJANGOH', 'GETaLM')
_PYTHIA_RE = re.compile(r'^[Pp]ythia[ _]?(\d)(.*)$')


def _split_gen_token(tok):
    """Split a '<Generator><Version>' token into (generator, generator_version),
    or (None, None) when there is no known generator *with a non-empty version*.

    A bare generator name (e.g. 'pythia8', 'eSTARlight') has no version in the
    source and resolves to (None, None) — left for manual association, not
    guessed. The leading 'v' of a version is preserved (EpIC 'v1.1.6-1.2');
    pythiaN keeps its major-version digit in both the family and the version.
    """
    t = (tok or '').strip()
    if not t:
        return None, None
    m = _PYTHIA_RE.match(t)
    if m:
        if not m.group(2).strip(' ._-'):     # bare 'pythia8' / 'Pythia 8'
            return None, None
        return f'pythia{m.group(1)}', f'{m.group(1)}{m.group(2)}'.lstrip('._- ')
    for g in _EVGEN_NAMES:
        if t.lower().startswith(g.lower()):
            ver = t[len(g):].lstrip('._- ')   # keep a leading 'v'
            return (g, ver) if ver else (None, None)
    return None, None


def derive_evgen(path, gen_version=''):
    """Curated (generator, generator_version) for a catalog row, or None when no
    confident resolution exists — left for manual association, never guessed.

    - ``EVGEN/SINGLE/...`` samples are the particle gun.
    - A background ``dataprod_rel`` release names no generator; the generator is
      the repository (e.g. EIC_ESR_Xsuite, EIC_SR_Geant4).
    - Otherwise a '<Generator><Version>' token from the gen_version release tag
      or the path is split; a bare generator with no version resolves to None.
    """
    segs = (path or '').split('/')
    if len(segs) > 1 and segs[0] == 'EVGEN' and segs[1] == 'SINGLE':
        return {'generator': 'particle_gun', 'generator_version': ''}
    gv = (gen_version or '').strip()
    # PYTHIA-RAD-CORR releases are bare versions and the path's pythia6 segment
    # is not a clean generator+version — ambiguous, left for manual association.
    if 'pythia-rad-corr' in gv.lower():
        return None
    release = gv.rstrip('/').split('/')[-1] if gv else ''
    if release.startswith('dataprod_rel') and 'github.com/' in gv:
        repo = gv.split('github.com/', 1)[1].split('/releases', 1)[0].split('/')[-1]
        if repo:
            return {'generator': repo, 'generator_version': release}
    for tok in [release, gv, *segs]:
        g, v = _split_gen_token(tok)
        if g:
            return {'generator': g, 'generator_version': v}
    return None


def single_particle_angle(path):
    """Angular-range tail of a single-particle path, or '' if none.

    Single-particle samples share a ``(particle, gun_energy)`` physics tag but
    differ by polar-angle range; the angle is a per-task detail, not part of the
    reusable tag (``derive_physics`` deliberately omits it). This returns the
    path tail after ``EVGEN/SINGLE/<particle>/<energy>/`` so the importer can
    store it as a per-task override. Returns '' for a non-single-particle path.
    """
    segs = (path or '').split('/')
    if len(segs) < 2 or segs[0] != 'EVGEN' or segs[1] != 'SINGLE':
        return ''
    return '/'.join(segs[4:])
