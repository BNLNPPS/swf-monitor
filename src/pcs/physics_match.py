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
