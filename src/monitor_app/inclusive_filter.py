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
page's query forward carries the keys. A link may also frame the page
with ``n``, the same syntax, pairs a row must ALL carry (an intersection,
what a count on another page means); clicks widen within that frame and
clear all drops it. A page that had one parameter per facet names them
in ``legacy``; those meant an intersection and are read into ``n``, so
old links open exactly what they opened before. The Match control
switches the selections between any (the union, the default) and all
(``match=all``, the intersection).

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


SEARCH_PARAM = 'q'
# The narrowing frame: selections a row must ALL carry, from links that
# name an intersection (the completion panel's cells, the former
# one-per-facet links). Clicks on the page add to f, never to n.
NARROW_PARAM = 'n'
# The match mode: ``any`` (the default, absent from the URL), a row shows
# when it carries any selection; ``all``, only when it carries every one.
MATCH_PARAM = 'match'
MATCH_ANY = 'any'
MATCH_ALL = 'all'


class InclusiveFilter:
    """The selections in ``query`` (``f``), matched as a union or, with
    ``match=all``, as an intersection; a narrowing frame (``n``, an
    intersection: every pair must hold); and a free-text search (``q``),
    applied in the browser over each row: a row shows when it carries
    every narrowing pair, matches any selection (every selection under
    ``match=all``; or none is made), and contains the search text. A
    page's former one-per-facet parameters meant an intersection, so they
    are read into the frame. ``noun`` names what a row is on the page
    ("configurations"), for the text the include shows."""

    def __init__(self, query, param=PARAM, legacy=None, noun='rows'):
        self.param = param
        self.legacy = dict(legacy or {})
        self.noun = noun
        self.match = (MATCH_ALL if query and (query.get(MATCH_PARAM) or '').strip() == MATCH_ALL
                      else MATCH_ANY)
        self.search = ((query.get(SEARCH_PARAM) or '').strip()
                       if query else '')
        self.selections = []
        self.narrow = []
        for item in self._raw(query, param).split(SEP):
            self._add(self.selections, item)
        for item in self._raw(query, NARROW_PARAM).split(SEP):
            self._add(self.narrow, item)
        for key, legacy_param in self.legacy.items():
            value = (query.get(legacy_param) or '').strip() if query else ''
            if value:
                self._add(self.narrow, f'{key}:{value}')

    @staticmethod
    def _raw(query, param):
        if not query:
            return ''
        if hasattr(query, 'getlist'):
            return SEP.join(v for v in query.getlist(param) if v)
        return query.get(param) or ''

    @staticmethod
    def _add(target, item):
        item = (item or '').strip()
        if not item or ':' not in item:
            return
        key, value = item.split(':', 1)
        pair = (key.strip(), value.strip())
        if pair[0] and pair[1] and pair not in target:
            target.append(pair)

    @property
    def active(self):
        return bool(self.selections or self.narrow)

    def selected(self, key):
        return {v for k, v in self.selections if k == key}

    def encode(self, selections=None):
        sel = self.selections if selections is None else selections
        return SEP.join(f'{k}:{v}' for k, v in sel)

    @property
    def echo(self):
        """The query fragment that reproduces this slice, for urlencode."""
        out = {}
        if self.narrow:
            out[NARROW_PARAM] = self.encode(self.narrow)
        if self.selections:
            out[self.param] = self.encode()
            if self.match == MATCH_ALL:
                out[MATCH_PARAM] = MATCH_ALL
        return out

    def _wanted(self):
        wanted = {}
        for key, value in self.selections:
            wanted.setdefault(key, set()).add(value)
        return wanted

    def matches(self, row, facets):
        """Whether the row carries every narrowing pair and ANY selected
        value (EVERY selected value under ``match=all``; or none is made)."""
        by_key = {f.key: f for f in facets}
        for key, value in self.narrow:
            facet = by_key.get(key)
            if facet is None or value not in facet.values(row):
                return False
        if not self.selections:
            return True
        if self.match == MATCH_ALL:
            return all(by_key.get(key) is not None and value in by_key[key].values(row)
                       for key, value in self.selections)
        for key, values in self._wanted().items():
            facet = by_key.get(key)
            if facet is not None and values & set(facet.values(row)):
                return True
        return False

    def apply(self, rows, facets):
        """The rows within the frame matching ANY selection (EVERY one
        under ``match=all``); all rows when nothing is in force."""
        if not self.active:
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

    def _url(self, request, selections, narrow=None, match=None):
        params = request.GET.copy()
        for legacy_param in self.legacy.values():
            params.pop(legacy_param, None)
        params.pop(self.param, None)
        params.pop(NARROW_PARAM, None)
        params.pop(MATCH_PARAM, None)
        narrow = self.narrow if narrow is None else narrow
        match = self.match if match is None else match
        if narrow:
            params[NARROW_PARAM] = self.encode(narrow)
        if selections:
            params[self.param] = self.encode(selections)
        if match == MATCH_ALL:
            params[MATCH_PARAM] = MATCH_ALL
        encoded = params.urlencode()
        return f'{request.path}?{encoded}' if encoded else request.path

    def match_url(self, request, match):
        return self._url(request, self.selections, match=match)

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
        return self._url(request, [], narrow=[])

    def _labelled(self, pairs, facets):
        by_key = {f.key: f for f in facets}
        out = []
        for key, value in pairs:
            facet = by_key.get(key)
            out.append((facet.label if facet else key,
                        facet.display(value) if facet else value))
        return out

    def narrow_filters(self, facets):
        """(label, shown value) per narrowing pair."""
        return self._labelled(self.narrow, facets)

    def union_filters(self, facets):
        """(label, shown value) per selection, in click order."""
        return self._labelled(self.selections, facets)

    def active_filters(self, facets):
        """Every filter in force, the narrowing pairs first: what a
        consumer states about the slice."""
        return self.narrow_filters(facets) + self.union_filters(facets)

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
            'narrow_filters': self.narrow_filters(facets),
            'union_filters': self.union_filters(facets),
            'clear_url': self.clear_url(request),
            'active': self.active,
            'param': self.param,
            'narrow_param': NARROW_PARAM,
            'match': self.match,
            'match_param': MATCH_PARAM,
            'match_any_url': self.match_url(request, MATCH_ANY),
            'match_all_url': self.match_url(request, MATCH_ALL),
            'noun': self.noun,
            'search': self.search,
            'search_param': SEARCH_PARAM,
            'legacy_json': json.dumps(sorted(self.legacy.values())),
            'selections_json': json.dumps([list(p) for p in self.selections]),
            'narrow_json': json.dumps([list(p) for p in self.narrow]),
        }


def echo_query(flt):
    """``&``-ready query string reproducing the slice, or ''."""
    return urlencode(flt.echo) if flt.active else ''
