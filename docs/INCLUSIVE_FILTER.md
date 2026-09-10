# Inclusive filter

A facet filter whose selections widen the shown set instead of narrowing
it. `monitor_app/inclusive_filter.py` and the include
`monitor_app/_inclusive_filter.html`; first used on the campaign plan
page (swf-epicprod `pcs/views.py`, `pcs_campaign_plan`).

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
  order: "Showing rows matching any of: Process: DIS · Beam: 10x100".

## URL contract

One parameter carries the state: `f`, the selections as `facet:value`
pairs joined by `|`, in click order.

    ?f=process:DIS|beam:10x100

A page that previously carried one parameter per facet lists them in
`legacy`; such a parameter is read as a selection and dropped from the
URLs the filter writes, so old links keep resolving. Consumers that
carry a page's filter state forward (the campaign plan's Time history
embed and the Snapper Campaign focus view) carry the one key.

Two parameters that used to mean an intersection (`priority=1` and
`status=below-target` from the production home completion panel) are
read as two selections and therefore a union. The panel's cell links
are affected; their treatment is undecided.

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
    rows = flt.apply(rows_all, facets)
    context['inclusive_filter'] = flt.context(rows_all, facets, request)

and in the template `{% include 'monitor_app/_inclusive_filter.html' %}`.

`Facet(key, label, values, display=None, order=None)`: `values(row)`
returns the value or values a row carries (several for a
multi-membership axis; None or empty for none); `display` maps a stored
value to its shown form; `order` is a list of values or a sort key,
else values sort. `InclusiveFilter.echo` is the query fragment that
reproduces the slice, for `urlencode`; `active_filters(facets)` the
(label, shown value) list; `toggle_url`, `clear_facet_url` and
`clear_url` the link builders the include uses.

## Pages

| Page | Since |
|---|---|
| Campaign plan (`/pcs/plan/`), both the plan and the assembly view | 2026-09-10 |

Other pages switch as decided, one at a time, by replacing their facet
construction with the three calls above.
