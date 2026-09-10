"""Inclusive filter: a facet filter whose selections widen, never narrow.

Clicking a value selects it and every value stays listed with its count.
Each further click adds that value's rows, whatever facet it belongs to:
the rows shown are the union of everything selected. Clicking a selected
value deselects it; a facet's All clears that facet; clear all clears
everything. With nothing selected every row is shown.

The selection is applied in the browser. The server renders every row
once, each carrying its facet values in a ``data-if`` attribute and the
initial hidden state from the URL, and the include's script does the
rest without a request: bold, show and hide, the shown count, the
statement, the URL. State rides one URL parameter, ``f``, the selections
in click order as ``facet:value`` pairs joined by ``|``
(``?f=process:DIS|beam:10x100``), rewritten with replaceState on each
click, so the page stays bookmarkable and a consumer that carries the
page's query forward carries one key. A page that had one parameter per
facet names them in ``legacy`` and old links keep working: each is read
as a selection and dropped from the URLs this filter writes.

Usage, on a view whose rows are plain mappings::

    facets = [Facet('process', 'Process', lambda r: r['process']), ...]
    flt = InclusiveFilter(request.GET, legacy={'process': 'process'})
    shown = flt.annotate(rows_all, facets)   # row['if_attr'], row['if_hidden']
    context['inclusive_filter'] = flt.context(rows_all, facets, request)

in the template ``{% include 'monitor_app/_inclusive_filter.html' %}``
above the table, and on every row
``<tr data-if="{{ r.if_attr }}"{% if r.if_hidden %} class="swf-if-hidden"{% endif %}>``.
An element with class ``swf-if-shown`` receives the shown count. A page
script that must follow the selection listens for the ``swf-if-change``
event on ``document`` (``detail.selections``, ``detail.query``,
``detail.shown``) and reads visible rows as ``tr[data-if]:not(.swf-if-hidden)``.
"""

import json
from urllib.parse import urlencode

PARAM = 'f'
SEP = '|'


class Facet:
    """One filter axis.

    ``values(row)`` returns the value or values the row carries in this
    facet; None or an empty iterable means none. ``display`` maps a value
    to what is shown; ``order`` fixes the listing order as a list of
    values or a sort key, else values sort.
    """

    def __init__(self, key, label, values, display=None, order=None):
        self.key = key
        self.label = label
        self._values = values
        self.display = display or (lambda v: v)
        self.order = order

    def values(self, row):
        got = self._values(row)
        if got is None or got == '':
            return ()
        if isinstance(got, (str, int, float)):
            return (str(got),)
        return tuple(str(v) for v in got if v is not None and v != '')


class InclusiveFilter:
    def __init__(self, query, param=PARAM, legacy=None):
        self.param = param
        self.legacy = dict(legacy or {})
        self.selections = []
        raw = ''
        if hasattr(query, 'getlist'):
            raw = SEP.join(v for v in query.getlist(param) if v)
        else:
            raw = (query.get(param) or '') if query else ''
        for item in raw.split(SEP):
            item = item.strip()
            if not item or ':' not in item:
                continue
            key, value = item.split(':', 1)
            self._add(key.strip(), value.strip())
        for key, legacy_param in self.legacy.items():
            value = (query.get(legacy_param) or '').strip() if query else ''
            if value:
                self._add(key, value)

    def _add(self, key, value):
        if key and value and (key, value) not in self.selections:
            self.selections.append((key, value))

    @property
    def active(self):
        return bool(self.selections)

    def selected(self, key):
        return {v for k, v in self.selections if k == key}

    def encode(self, selections=None):
        sel = self.selections if selections is None else selections
        return SEP.join(f'{k}:{v}' for k, v in sel)

    @property
    def echo(self):
        """The query fragment that reproduces this slice, for urlencode."""
        return {self.param: self.encode()} if self.selections else {}

    def _wanted(self):
        wanted = {}
        for key, value in self.selections:
            wanted.setdefault(key, set()).add(value)
        return wanted

    def matches(self, row, facets):
        """Whether the row carries ANY selected value; True with none made."""
        if not self.selections:
            return True
        by_key = {f.key: f for f in facets}
        for key, values in self._wanted().items():
            facet = by_key.get(key)
            if facet is not None and values & set(facet.values(row)):
                return True
        return False

    def apply(self, rows, facets):
        """The rows matching ANY selection; all rows when none is made."""
        if not self.selections:
            return list(rows)
        return [row for row in rows if self.matches(row, facets)]

    def row_values(self, row, facets):
        """{facet key: [values]} the row carries, the browser's copy of
        what the facets see."""
        out = {}
        for facet in facets:
            values = facet.values(row)
            if values:
                out[facet.key] = list(values)
        return out

    def row_attr(self, row, facets):
        """The row's ``data-if`` attribute value (JSON)."""
        return json.dumps(self.row_values(row, facets), separators=(',', ':'))

    def annotate(self, rows, facets):
        """Set ``if_attr`` and ``if_hidden`` on every row (plain mappings)
        for the template; returns the number initially shown."""
        shown = 0
        for row in rows:
            row['if_attr'] = self.row_attr(row, facets)
            row['if_hidden'] = not self.matches(row, facets)
            shown += 0 if row['if_hidden'] else 1
        return shown

    def _url(self, request, selections):
        params = request.GET.copy()
        for legacy_param in self.legacy.values():
            params.pop(legacy_param, None)
        params.pop(self.param, None)
        if selections:
            params[self.param] = self.encode(selections)
        encoded = params.urlencode()
        return f'{request.path}?{encoded}' if encoded else request.path

    def toggle_url(self, request, key, value):
        pair = (key, value)
        if pair in self.selections:
            sel = [p for p in self.selections if p != pair]
        else:
            sel = self.selections + [pair]
        return self._url(request, sel)

    def clear_facet_url(self, request, key):
        return self._url(request, [p for p in self.selections if p[0] != key])

    def clear_url(self, request):
        return self._url(request, [])

    def active_filters(self, facets):
        """(label, shown value) per selection, in click order."""
        by_key = {f.key: f for f in facets}
        out = []
        for key, value in self.selections:
            facet = by_key.get(key)
            out.append((facet.label if facet else key,
                        facet.display(value) if facet else value))
        return out

    def facet_rows(self, rows_all, facets, request):
        """Every value of every facet with its count over all rows, its
        toggle URL and whether it is selected. A selected value with no
        rows stays listed at zero; nothing disappears."""
        out = []
        for facet in facets:
            counts = {}
            for row in rows_all:
                for value in facet.values(row):
                    counts[value] = counts.get(value, 0) + 1
            for value in self.selected(facet.key):
                counts.setdefault(value, 0)
            if callable(facet.order):
                keys = sorted(counts, key=facet.order)
            elif facet.order:
                order = {v: i for i, v in enumerate(facet.order)}
                keys = sorted(counts, key=lambda v: (order.get(v, len(order)), v))
            else:
                keys = sorted(counts)
            chosen = self.selected(facet.key)
            out.append((facet.label, {
                'key': facet.key,
                'items': [{'value': facet.display(v), 'raw': v,
                           'count': counts[v],
                           'url': self.toggle_url(request, facet.key, v),
                           'active': v in chosen}
                          for v in keys],
                'all_url': self.clear_facet_url(request, facet.key),
                'all_active': not chosen,
            }))
        return out

    def context(self, rows_all, facets, request):
        """The template include's context: facet rows, the active list,
        the clear-all URL, and the state the include's script starts
        from (the parameter name, the legacy parameters it drops from
        the URL, the selections)."""
        return {
            'facet_rows': self.facet_rows(rows_all, facets, request),
            'active_filters': self.active_filters(facets),
            'clear_url': self.clear_url(request),
            'active': self.active,
            'param': self.param,
            'legacy_json': json.dumps(sorted(self.legacy.values())),
            'selections_json': json.dumps([list(p) for p in self.selections]),
        }


def echo_query(flt):
    """``&``-ready query string reproducing the slice, or ''."""
    return urlencode(flt.echo) if flt.active else ''
