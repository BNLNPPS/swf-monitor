"""HTML cell-formatting helpers for DataTables ajax responses.

Shared across view modules so any table can emit state-colored cells
without duplicating the wrapping convention.
"""

from .state_descriptions import state_description


def fill_cell(content, state, url=None):
    """Wrap content so the enclosing <td> fills with the BigMon state color.

    The content is wrapped in ``<span data-fill="<state>_fill">…</span>``.
    The DataTables `createdCell` hook in _datatable_base.html /
    _datatable_dynamic_base.html reads td.innerHTML, extracts the
    data-fill value, and promotes it as a class on the <td> itself so
    the whole cell fills with the state color (see
    src/monitor_app/static/css/state-colors.css).

    When the state has a known description (see state_descriptions.py), a
    ``title=`` attribute is added so hovering the cell reveals what the
    state means. Unknown states omit the tooltip silently.

    ``state`` can be any short label that resolves to a ``<label>_fill``
    CSS class (e.g. a PanDA status, a log level, an agent status).
    Empty/falsy state returns the content unwrapped (no fill). The class
    is lowercased to match the BigMon CSS convention.
    """
    if content is None:
        content = ''
    if not state:
        return str(content)
    fill = f'{str(state).lower()}_fill'
    title_attr = ''
    desc = state_description(state)
    if desc:
        # Escape quotes defensively — descriptions are static but the path
        # is ajax-rendered and any change should be safe.
        safe_desc = str(desc).replace('"', '&quot;')
        title_attr = f' title="{safe_desc}"'
    wrapper = f'<span data-fill="{fill}"{title_attr}>{content}</span>'
    if url:
        return f'<a href="{url}" style="text-decoration:none;color:inherit;">{wrapper}</a>'
    return wrapper
