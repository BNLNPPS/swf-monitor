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


def unit_segments(unit, origin, observed_end):
    """Partition one unit without treating an unmeasured interval as processing."""
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
    if reconstruction.get('initialization_started_at'):
        add('initialization', reconstruction['initialization_started_at'],
            reconstruction.get('initialized_at') or end, 3)
    if reconstruction.get('started_at'):
        add('reconstruction', reconstruction['started_at'], reconstruction.get('ended_at') or end, 2)
    for name in ('simulation', 'reconstruction'):
        phase = phases.get(name) or {}
        separated = (bool(simulation.get('first_event_at')) if name == 'simulation'
                     else bool(reconstruction.get('started_at')))
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
