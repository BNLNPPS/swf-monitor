"""Measured Event Service phases, with aggregate legacy time kept explicit."""
from datetime import datetime, timezone


def epoch(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def pooled(unit):
    """True when the unit's reconstruction ran on a shared pool member
    (swf-epicprod es/reco_pool.py), not on the unit's own core."""
    return (unit.get('reconstruction') or {}).get('pool_member') is not None


def member_segments(unit, origin, observed_end):
    """A pooled unit's reconstruction on its member's lane: the member's
    startup when this unit's request started it, then the request."""
    reconstruction = unit.get('reconstruction') or {}
    parts = []
    for phase, first, last in (('initialization', reconstruction.get('initialization_started_at'),
                                reconstruction.get('initialized_at')),
                               ('reconstruction', reconstruction.get('started_at'),
                                reconstruction.get('ended_at'))):
        first, last = epoch(first), epoch(last)
        if phase == 'initialization' and int(reconstruction.get('pool_requests') or 1) != 1:
            continue
        if first is None or last is None:
            continue
        first, last = max(origin, first), min(observed_end, last)
        if last > first:
            parts.append({'phase': phase, 'start_s': first - origin, 'end_s': last - origin})
    return parts


def unit_segments(unit, origin, observed_end, without_reconstruction=False):
    """Partition one unit without treating an unmeasured interval as processing.
    without_reconstruction: the reconstruction ran on a shared member, so it is
    no part of this core's time (member_segments draws it on the member's lane)."""
    start = max(origin, epoch(unit['started_at']))
    end = min(observed_end, epoch(unit.get('ended_at')) or observed_end)
    if end <= start:
        return []
    candidates = []

    def add(phase, first, last, priority):
        first, last = epoch(first), epoch(last)
        if first is None or last is None:
            return
        first, last = max(start, first), min(end, last)
        if last > first:
            candidates.append((first, last, phase, priority))

    simulation = unit.get('simulation') or {}
    reconstruction = unit.get('reconstruction') or {}
    phases = unit.get('phases') or {}
    # The first event includes the lazy Geant4 first-run setup in startup.
    if simulation.get('initialization_started_at') and int(simulation.get('sequence') or 1) == 1:
        add('initialization', simulation['initialization_started_at'],
            simulation.get('first_event_at') or end, 3)
    if simulation.get('first_event_at'):
        add('simulation', simulation['first_event_at'],
            simulation.get('last_event_at') or simulation.get('ended_at') or end, 2)
    if reconstruction.get('initialization_started_at') and not without_reconstruction:
        add('initialization', reconstruction['initialization_started_at'],
            reconstruction.get('initialized_at') or end, 3)
    if reconstruction.get('started_at') and not without_reconstruction:
        add('reconstruction', reconstruction['started_at'], reconstruction.get('ended_at') or end, 2)
    for name in ('simulation', 'reconstruction'):
        phase = phases.get(name) or {}
        separated = (bool(simulation.get('first_event_at')) if name == 'simulation'
                     else bool(reconstruction.get('started_at')))
        if name == 'reconstruction' and without_reconstruction:
            continue
        if phase.get('started_at') and not separated:
            add('unseparated', phase['started_at'], phase.get('ended_at') or end, 1)
    measured = bool(simulation or reconstruction or phases)
    add('overhead' if measured else 'unseparated', start, end, 0)
    boundaries = sorted({point for a, b, _, _ in candidates for point in (a, b)})
    result = []
    for first, last in zip(boundaries, boundaries[1:]):
        middle = (first + last) / 2
        covering = [part for part in candidates if part[0] <= middle < part[1]]
        if not covering:
            continue
        phase = max(covering, key=lambda part: part[3])[2]
        if result and result[-1]['phase'] == phase:
            result[-1]['end_s'] = last - origin
        else:
            result.append({'phase': phase, 'start_s': first - origin, 'end_s': last - origin})
    return result


def turnaround(lanes):
    """On each core, the seconds from the last measured event processing of
    one unit to the first of the next: what a core spends between segments.
    A unit without measured processing breaks the sequence rather than
    joining two gaps into one."""
    gaps = []
    for units in lanes:
        previous = None
        for unit in sorted(units, key=lambda u: u['start_s']):
            processing = [part for part in unit.get('segments') or []
                          if part['phase'] in ('simulation', 'reconstruction')]
            if not processing:
                previous = None
                continue
            if previous is not None:
                gaps.append(max(0., processing[0]['start_s'] - previous))
            previous = processing[-1]['end_s']
    if not gaps:
        return {'turnaround_n': 0}
    gaps.sort()

    def quantile(q):
        return round(gaps[min(len(gaps) - 1, int(q * len(gaps)))], 2)

    return {'turnaround_n': len(gaps), 'turnaround_median_s': quantile(0.5),
            'turnaround_p90_s': quantile(0.9), 'turnaround_max_s': round(gaps[-1], 2)}


def phase_totals(units):
    result = dict(processing_s=0., saved_processing_s=0., initialization_s=0.,
                  overhead_s=0., unseparated_s=0., measured_units=0)
    for unit in units:
        separated = False
        for part in unit.get('segments') or []:
            duration = max(0., part['end_s'] - part['start_s'])
            phase = part['phase']
            if phase in ('simulation', 'reconstruction'):
                separated = True
                result['processing_s'] += duration
                if unit['status'] == 'done':
                    result['saved_processing_s'] += duration
            elif phase == 'initialization':
                separated = True
                result['initialization_s'] += duration
            else:
                result[phase + '_s'] += duration
        result['measured_units'] += int(separated)
    return {key: round(value, 3) for key, value in result.items()}
