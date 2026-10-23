"""Narrowing filter: the facet filter of the operations views.

A selection narrows: the rows shown are the intersection of every
selected value, one value per facet, one URL parameter per facet
(``?class=sparse&queue=NERSC_Perlmutter_epic``), plus a free-text
search (``q``). Every facet bar counts the distribution within the
selection: a facet's own bar counts within the other facets'
selections, so its alternatives stay listed with the count each would
give, and the other bars count within the full selection, which is the
question an operations view asks: what does this slice look like.

The inclusive filter (``inclusive_filter.py``) is the other house
filter, for views where selections widen and every value keeps its
full count. docs/INCLUSIVE_FILTER.md § The narrowing filter.

Usage::

    from monitor_app.narrowing_filter import Facet, NarrowingFilter

    facets = [
        Facet('class', 'Class', lambda r: r['class'],
              display=lambda v: labels.get(v, v), order=[...]),
        Facet('queue', 'Queue', lambda r: r['queues']),
    ]
    flt = NarrowingFilter(request.GET)
    rows = flt.apply(rows_all, facets)
    context['narrowing_filter'] = flt.context(rows_all, facets, request)

and in the template, ``{% include 'monitor_app/_narrowing_filter.html' %}``
above the table, which renders the bars, the search box and the
active-filters line; every row carries ``data-nf="{{ r.nf_attr }}"`` and
the class ``swf-nf-hidden`` when ``r.nf_hidden``. The server renders the
full row set with the request's selection applied (the no-script state,
and what a link opens); the include's script applies every click in the
browser: hides and shows rows, recounts every bar within the selection,
rewrites the URL with ``replaceState``. A click costs no request.
"""
import json
from collections import Counter
from urllib.parse import urlencode

from .inclusive_filter import Facet  # the same facet definition serves both

SEARCH_PARAM = 'q'

__all__ = ['Facet', 'NarrowingFilter', 'SEARCH_PARAM']


class NarrowingFilter:
    """The selections in ``query``, one value per facet key, and the
    search text; the rows shown are those carrying every selection and
    containing the text."""

    def __init__(self, query, facets=None, search_param=SEARCH_PARAM):
        self.search_param = search_param
        self.search = ((query.get(search_param) or '').strip() if query else '')
        self._query = query
        self.selected = {}
        if facets is not None:
            self.read(facets)

    def read(self, facets):
        """Read one value per facet from the query."""
        for f in facets:
            value = (self._query.get(f.key) or '').strip() if self._query else ''
            self.selected[f.key] = value
        return self

    # ---------------------------------------------------------- matching

    def matches(self, row, facets, skip=None):
        """Whether ``row`` carries every selected value (the facet ``skip``
        excepted) and contains the search text."""
        for f in facets:
            want = self.selected.get(f.key, '')
            if want and f.key != skip and want not in f.values(row):
                return False
        if self.search:
            text = json.dumps(row, default=str).lower()
            if self.search.lower() not in text:
                return False
        return True

    def apply(self, rows, facets):
        """The rows the selection keeps."""
        if not self.selected:
            self.read(facets)
        return [r for r in rows if self.matches(r, facets)]

    def row_values(self, row, facets):
        return {f.key: list(f.values(row)) for f in facets}

    def annotate(self, rows, facets):
        """Set ``nf_attr`` (the row's facet values, JSON) and ``nf_hidden``
        (whether the current selection hides it) on every row, so the page
        renders every row once and the browser applies each click; returns
        the number initially shown."""
        if not self.selected:
            self.read(facets)
        shown = 0
        for row in rows:
            row['nf_attr'] = json.dumps(self.row_values(row, facets), separators=(',', ':'))
            row['nf_hidden'] = not self.matches(row, facets)
            shown += 0 if row['nf_hidden'] else 1
        return shown

    # --------------------------------------------------------- the bars

    def url(self, changes):
        """The query string for the current selection with ``changes``
        applied (a value of '' clears the key)."""
        params = {k: v for k, v in self.selected.items() if v}
        if self.search:
            params[self.search_param] = self.search
        for k, v in changes.items():
            if v:
                params[k] = v
            else:
                params.pop(k, None)
        return '?' + urlencode(params) if params else '?'

    def bars(self, rows_all, facets):
        """One bar per facet: its values with the count each would give
        within the other facets' selections, the selected one marked."""
        if not self.selected:
            self.read(facets)
        bars = []
        for f in facets:
            pool = [r for r in rows_all if self.matches(r, facets, skip=f.key)]
            counts = Counter(v for r in pool for v in f.values(r))
            if isinstance(f.order, (list, tuple)):
                rank = {v: i for i, v in enumerate(f.order)}
                ordered = sorted(counts, key=lambda v: (rank.get(v, len(rank)), v))
            elif callable(f.order):
                ordered = sorted(counts, key=f.order)
            else:
                ordered = sorted(counts)
            selected = self.selected.get(f.key, '')
            bars.append({
                'key': f.key, 'label': f.label, 'selected': selected,
                'all_url': self.url({f.key: ''}),
                'all_active': not selected,
                # A click on the selected value clears it, as in the
                # inclusive filter; any other value replaces the selection.
                'items': [{'value': v, 'display': f.display(v), 'count': counts[v],
                           'url': self.url({f.key: '' if selected == v else v}),
                           'active': selected == v}
                          for v in ordered],
            })
        return bars

    def active_filters(self, facets):
        """[{label, value}] of the selections made, for the active line."""
        out = []
        for f in facets:
            value = self.selected.get(f.key, '')
            if value:
                out.append({'label': f.label, 'value': f.display(value)})
        if self.search:
            out.append({'label': 'Search', 'value': self.search})
        return out

    def context(self, rows_all, facets, request):
        """The include's context: the bars as the server counts them for
        this request (the no-script state), plus what the browser script
        needs to recount and rewrite the URL on every click: the facet
        keys, labels and display names, and the selection."""
        return {
            'bars': self.bars(rows_all, facets),
            'search': self.search,
            'search_param': self.search_param,
            'hidden': [(k, v) for k, v in self.selected.items() if v],
            'active_filters': self.active_filters(facets),
            'clear_url': request.path,
            'total': len(rows_all),
            'facets_json': json.dumps([
                {'key': f.key, 'label': f.label,
                 'display': {v: f.display(v) for r in rows_all for v in f.values(r)},
                 'order': [str(v) for v in f.order] if isinstance(f.order, (list, tuple)) else None}
                for f in facets], separators=(',', ':')),
            'selected_json': json.dumps({k: v for k, v in self.selected.items() if v},
                                        separators=(',', ':')),
        }
