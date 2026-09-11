# Inclusive filter

A facet filter whose selections widen the shown set instead of narrowing
it, applied in the browser. `monitor_app/inclusive_filter.py` and the
include `monitor_app/_inclusive_filter.html`; first used on the campaign
plan page (swf-epicprod `pcs/views.py`, `pcs_campaign_plan`).

## Behavior

- Every facet lists every value with its count over all rows. Nothing
  disappears when a selection is made; a selected value with no rows
  stays listed at zero.
- Clicking a value selects it, shown bold and underlined. The rows shown
  are the union of every selected value, across facets as well as
  within one: Process DIS plus Beam 10x100 shows the rows that are DIS
  or at 10x100.
- Clicking a selected value deselects it. A facet's All clears that
  facet. Clear all clears everything. With nothing selected every row
  is shown.
- The statement under the facet rows names the selections in click
  order: "Showing rows matching any of: Process: DIS Beam: 10x100".
- A click costs no request. The server renders every row once with its
  facet values and the initial hidden state; the include's script
  applies each click to the page: the bold marks, the rows, the shown
  count, the statement, the URL. On the campaign plan, 1070 rows, the
  table changes in about 50 ms.

## URL contract

One parameter carries the state: `f`, the selections as `facet:value`
pairs joined by `|`, in click order, rewritten with `replaceState` on
every click, so the address bar always holds the current state and a
bookmark opens it. Back leaves the page.

    ?f=process:DIS|beam:10x100

A link may frame the page with `n`, the same syntax: pairs every row
must carry, an intersection, which is what a count on another page
means (the completion panel's "priority 1, below target" cell opens
exactly the rows it counts). Clicks widen within the frame; clear all
drops it. The statement reads "Showing rows within Priority: 1 Status:
below target matching any of: ...".

A page that previously carried one parameter per facet lists them in
`legacy`. Those parameters meant an intersection, so they are read into
the frame and dropped from the URLs the filter writes; old links open
exactly what they opened before. Consumers that carry a page's filter
state forward (the campaign plan's Time history embed and the Snapper
Campaign focus view) carry `n`, `f` and `q`. A value containing `|`
cannot be encoded; no filtered value on the plan carries one.

## Usage

    from monitor_app.inclusive_filter import Facet, InclusiveFilter

    facets = [
        Facet('process', 'Process', lambda r: r['process']),
        Facet('requestor', 'Requestor',
              lambda r: r['requestors'] or ['Unassigned']),
        Facet('status', 'Status', lambda r: slug_of.get(r['status']),
              display=lambda v: label_of.get(v, v), order=[...slugs...]),
    ]
    flt = InclusiveFilter(request.GET, legacy={'process': 'process'})
    shown = flt.annotate(rows_all, facets)     # row['if_attr'], row['if_hidden']
    context['inclusive_filter'] = flt.context(rows_all, facets, request)

In the template, the include above the table and two things on every
row:

    {% include 'monitor_app/_inclusive_filter.html' %}
    ...
    <tr data-if="{{ r.if_attr }}"{% if r.if_hidden %} class="swf-if-hidden"{% endif %}>

An element with class `swf-if-shown` receives the shown count. Page
scripts that must follow the selection listen for the `swf-if-change`
event on `document`, whose `detail` carries `selections`, `query` (the
`f=...` fragment, URL-encoded) and `shown`, and read the visible rows as
`tr[data-if]:not(.swf-if-hidden)`. A page with its own row selection
(tick boxes, select all) restricts it to visible rows the same way.

Rows live in the house sortable tables (DataTables through
`swf-sortable`), which move row nodes on a sort and never recreate them,
so the hidden class survives sorting; the include restripes the visible
rows after each change and after each sort.

`Facet(key, label, values, display=None, order=None)`: `values(row)`
returns the value or values a row carries (several for a
multi-membership axis; None or empty for none); `display` maps a stored
value to its shown form; `order` is a list of values or a sort key,
else values sort. `InclusiveFilter.echo` is the query fragment that
reproduces the slice, for `urlencode`; `matches`, `apply`,
`active_filters`, and the link builders `toggle_url`, `clear_facet_url`
and `clear_url` serve the server side and the no-script fallback: the
anchors keep server URLs, so a middle click opens the right page.

## Pages

| Page | Since |
|---|---|
| Campaign plan (`/pcs/plan/`), both the plan and the assembly view; its delivery map follows the selection through the Snapper embed's `setHidden` hook (snapper-ai docs/INTEGRATION.md § 4) | 2026-09-10 |

Other pages switch as decided, one at a time, by replacing their facet
construction with the calls above.

## The narrowing filter

The other house filter, for the operations views: a selection narrows.
`monitor_app/narrowing_filter.py` and the include
`monitor_app/_narrowing_filter.html`; first used on the segfault catalog
(`monitor_app/viewdir/pandamon.py`, `panda_segfaults`).

The two filters answer different questions. The inclusive filter serves
a view where the reader assembles the set to look at (the campaign
plan): selections widen, every value keeps its full count. The
narrowing filter serves a view where the reader asks what a slice looks
like (the operations views): a selection narrows the rows to the
intersection, and every bar re-counts within the selection. A facet's
own bar counts within the other facets' selections, so its alternatives
stay listed with the count each would give; the other bars count
within the whole selection. Picking a queue on the catalog leaves the
class bar showing the classes of that queue's crashes.

### URL contract

One parameter per facet, its key the facet key, one value
(`?class=sparse&queue=NERSC_Perlmutter_epic`); `q` a free-text search
over every column. A click on the selected value clears it, as in the
inclusive filter; a click on another value replaces the selection. Every bar anchor is a server URL carrying the whole
selection, so the filter works without script and a middle click opens
the right page. Clear all is the page's path.

### Usage

    from monitor_app.narrowing_filter import Facet, NarrowingFilter

    facets = [
        Facet('class', 'Class', lambda r: r['class'],
              display=lambda v: labels.get(v, v), order=[...values...]),
        Facet('queue', 'Queue', lambda r: r['queues'] or None),
    ]
    flt = NarrowingFilter(request.GET, facets)
    rows = flt.apply(rows_all, facets)
    context['narrowing_filter'] = flt.context(rows_all, facets, request)

In the template, `{% include 'monitor_app/_narrowing_filter.html' %}`
above the table renders the bars in the house filter-bar markup, the
search box (which submits the current selection with it), and the
active-filters line. `Facet` is the same class both filters use.

### Pages

| Page | Since |
|---|---|
| Segfault catalog (`/panda/segfaults/`) | 2026-09-11 |

The ePIC queues page and the physics-configuration page carry their own
inline bars, counted over the full set; they switch when named.
