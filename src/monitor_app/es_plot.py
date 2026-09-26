"""The slot occupancy plot of an Event Service job: one lane per slot
along the job's own clock, each unit a bar (green done, red failed), the slot's start before its
first unit and drain after its last in yellow (the allocation lost),
each slot's processed events at its right and their total on the closes
lane, the closes on their own lane, the harness's span, the pilot's head and
tail as the bare lanes before the first unit and after the last. What
the batch slot was doing every minute of its life, and what it was not
(swf-epicprod NODE_EVENT_DISPATCHER.md, The record). Inline SVG, no
script, legible in both themes.
"""
from django.utils.html import escape
from django.utils.safestring import mark_safe

LEFT = 70          # the lane labels
RIGHT = 90         # each slot's events
WIDTH = 1000
LANE_H = 20
AXIS_H = 34
TOP = 8

DONE = '#2e8b57'
FAILED = '#c0392b'
CLOSE = '#3a7bd5'
HARNESS = '#8e6bbf'
LOST = '#e0b400'   # a slot's allocation before its first unit and after its last
IDLE = 'rgba(128,128,128,0.18)'


def _tick_step(wall_s):
    """The minute step of the axis ticks for a wall of wall_s seconds."""
    minutes = wall_s / 60.0
    for step in (1, 2, 5, 10, 15, 30, 60, 120, 240):
        if minutes / step <= 12:
            return step
    return 480


def slot_plot_svg(tl):
    """The SVG for a timeline from queries.es_slot_timeline, or ''."""
    if not tl or not tl.get('wall_s'):
        return ''
    wall = float(tl['wall_s'])
    rows = list(tl.get('rows') or [])
    lanes = len(rows) + 1                         # the closes lane
    height = TOP + lanes * LANE_H + AXIS_H
    plot_w = WIDTH - LEFT - RIGHT

    def x(t):
        return LEFT + max(0.0, min(1.0, t / wall)) * plot_w

    parts = [f'<svg viewBox="0 0 {WIDTH} {height}" width="100%" height="{height}" '
             f'role="img" aria-label="slot occupancy" style="font-family:inherit;font-size:14px;max-width:100%">']
    # Lanes: the idle band the whole job long, then the units.
    for i, row in enumerate(rows):
        y = TOP + i * LANE_H
        parts.append(f'<text x="{LEFT - 8}" y="{y + LANE_H * 0.72:.1f}" text-anchor="end" fill="currentColor">slot {row["index"]}</text>')
        parts.append(f'<rect x="{LEFT}" y="{y + 3}" width="{plot_w:.1f}" height="{LANE_H - 6}" fill="{IDLE}"/>')
        # What the slot loses: its start before the first unit and its
        # drain after the last, the allocation it holds without work.
        if row['units']:
            first = min(u['start_s'] for u in row['units'])
            last = max(u['end_s'] for u in row['units'])
            for a, b, what in ((0.0, first, 'start'), (last, wall, 'drain')):
                if b > a:
                    title = escape(f"lost to the slot's {what}: {(b - a) / 60:.1f} min")
                    parts.append(f'<rect x="{x(a):.1f}" y="{y + 3}" width="{max(1.0, x(b) - x(a)):.1f}" '
                                 f'height="{LANE_H - 6}" fill="{LOST}"><title>{title}</title></rect>')
        for u in row['units']:
            x0, x1 = x(u['start_s']), x(u['end_s'])
            color = DONE if u['status'] == 'done' else FAILED
            title = escape(f"{u.get('unit_id') or 'unit'}: {u['status']}, {u.get('events') or 0} events, "
                           f"{(u['end_s'] - u['start_s']) / 60:.1f} min from {u['start_s'] / 60:.1f} min")
            parts.append(f'<rect x="{x0:.1f}" y="{y + 3}" width="{max(1.5, x1 - x0):.1f}" height="{LANE_H - 6}" '
                         f'fill="{color}"><title>{title}</title></rect>')
        # The events the slot processed: unequal counts show the stream
        # filling each slot by its own pace.
        n = sum(int(u.get('events') or 0) for u in row['units'] if u['status'] == 'done')
        parts.append(f'<text x="{WIDTH - 4}" y="{y + LANE_H * 0.72:.1f}" text-anchor="end" fill="currentColor">'
                     f'{n:,} ev</text>')
    # The closes lane.
    y = TOP + len(rows) * LANE_H
    total = sum(int(u.get('events') or 0) for row in rows for u in row['units'] if u['status'] == 'done')
    parts.append(f'<text x="{WIDTH - 4}" y="{y + LANE_H * 0.72:.1f}" text-anchor="end" fill="currentColor" '
                 f'font-weight="bold">{total:,} ev</text>')
    parts.append(f'<text x="{LEFT - 8}" y="{y + LANE_H * 0.72:.1f}" text-anchor="end" fill="currentColor">closes</text>')
    parts.append(f'<rect x="{LEFT}" y="{y + 3}" width="{plot_w:.1f}" height="{LANE_H - 6}" fill="{IDLE}"/>')
    for c in tl.get('closes') or []:
        x0, x1 = x(c['start_s']), x(c['end_s'])
        title = escape(f"close {c.get('index')}: {'registered' if c.get('ok') else 'failed'}, "
                       f"{(c['end_s'] - c['start_s']):.0f} s from {c['start_s'] / 60:.1f} min")
        parts.append(f'<rect x="{x0:.1f}" y="{y + 3}" width="{max(2.0, x1 - x0):.1f}" height="{LANE_H - 6}" '
                     f'fill="{CLOSE if c.get("ok") else FAILED}"><title>{title}</title></rect>')
    # The harness's span, a thin line under the lanes.
    h = tl.get('harness')
    y_axis = TOP + lanes * LANE_H
    if h and h.get('start_s') is not None:
        x0 = x(h['start_s'])
        x1 = x(h['end_s']) if h.get('end_s') is not None else x(wall)
        parts.append(f'<rect x="{x0:.1f}" y="{y_axis + 2}" width="{max(1.0, x1 - x0):.1f}" height="3" fill="{HARNESS}">'
                     f'<title>harness running: {h["start_s"] / 60:.1f} to {(h["end_s"] or wall) / 60:.1f} min</title></rect>')
    # The axis: minutes from the pilot's start of the job.
    step = _tick_step(wall)
    parts.append(f'<line x1="{LEFT}" y1="{y_axis + 8}" x2="{WIDTH - RIGHT}" y2="{y_axis + 8}" stroke="currentColor" stroke-opacity="0.5"/>')
    m = 0
    while m * 60 <= wall + 1e-6:
        xt = x(m * 60)
        parts.append(f'<line x1="{xt:.1f}" y1="{y_axis + 8}" x2="{xt:.1f}" y2="{y_axis + 13}" stroke="currentColor" stroke-opacity="0.5"/>')
        parts.append(f'<text x="{xt:.1f}" y="{y_axis + 27}" text-anchor="middle" fill="currentColor">{m}</text>')
        m += step
    parts.append(f'<text x="{WIDTH - 4}" y="{y_axis + 27}" text-anchor="end" fill="currentColor" fill-opacity="0.7">min</text>')
    parts.append('</svg>')
    return mark_safe(''.join(parts))
