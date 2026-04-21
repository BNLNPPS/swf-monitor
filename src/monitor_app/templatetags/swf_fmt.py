"""Display filters for swf-monitor templates.

Usage: ``{% load swf_fmt %}`` then ``{{ value|fmt_dt }}``.

Everything in the monitor shows times as Eastern (BNL local). Raw ISO
datetimes with microseconds leak through into detail pages — this gives
every template a single, cheap way to format them without each view
having to pre-process the values.
"""
from datetime import datetime, date
from zoneinfo import ZoneInfo

from django import template
from django.utils.safestring import mark_safe

from monitor_app.panda.constants import TASK_STATE_COLORS, JOB_STATE_COLORS
from monitor_app.state_descriptions import state_description as _state_description

register = template.Library()

_EASTERN = ZoneInfo('America/New_York')

_UNKNOWN_STATE_COLOR = '#6c757d'  # neutral gray fallback


@register.filter(name='fmt_dt')
def fmt_dt(value):
    """Format a datetime / ISO string as ``YYYYMMDD HH:MM:SS`` in Eastern.

    Returns:
        - '' for falsy input
        - the original string if parsing fails (don't hide bad data)
        - formatted string otherwise
    """
    if not value:
        return ''
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_EASTERN)
        return value.astimezone(_EASTERN).strftime('%Y%m%d %H:%M:%S')
    if isinstance(value, date):
        return value.strftime('%Y%m%d')
    return str(value)


def _badge(status, colors):
    """Return an HTML-safe colored status badge matching view-side _status_badge."""
    if not status:
        return ''
    color = colors.get(str(status).lower(), _UNKNOWN_STATE_COLOR)
    return mark_safe(
        f'<span style="background:{color};color:#fff;padding:2px 8px;'
        f'border-radius:3px;font-size:0.85em;">{status}</span>'
    )


@register.filter(name='task_badge')
def task_badge(status):
    """Colored badge for a task state (BigMon palette)."""
    return _badge(status, TASK_STATE_COLORS)


@register.filter(name='job_badge')
def job_badge(status):
    """Colored badge for a job state (BigMon palette)."""
    return _badge(status, JOB_STATE_COLORS)


@register.filter(name='state_class')
def state_class(value):
    """Return the BigMon `_fill` CSS class name for a state value.

    Use as ``<td class="{{ task.status|state_class }}">…</td>`` to fill
    the whole cell with the state's color per BigMon's state-colors.css.
    Lowercase the value so e.g. status 'Failed' still matches .failed_fill.
    """
    if not value:
        return ''
    return f'{str(value).lower()}_fill'


@register.filter(name='state_title')
def state_title(value):
    """Return the short human description for a state value, or '' if unknown.

    Use as ``<td class="{{ x.status|state_class }}" title="{{ x.status|state_title }}">``
    to attach a native browser tooltip to a colored state cell. Descriptions
    live in ``monitor_app.state_descriptions.STATE_DESCRIPTIONS``.
    """
    return _state_description(value)


@register.simple_tag(name='copy_btn')
def copy_btn(value):
    """One-click clipboard button for an ID/value.

    Usage: ``{% copy_btn task.jeditaskid %}`` next to the value you want to
    expose for copy. Rendered as a tiny Bootstrap-Icons clipboard button;
    global click handler in base.html copies ``value`` to the clipboard and
    flashes a check for 1s.
    """
    if not value:
        return ''
    # value goes into an HTML attribute; escape quotes and < >
    import html
    safe = html.escape(str(value), quote=True)
    return mark_safe(
        f'<button type="button" class="copy-btn" data-copy="{safe}" '
        f'title="Copy {safe}" aria-label="Copy">'
        f'<i class="bi bi-clipboard"></i></button>'
    )
