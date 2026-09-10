# The user view

The user view is the reduced face of epicprod for collaborators: the
same pages with less on them, behind one parameter. It is entered from
the bold **User view** link before Requests in the production nav, or
from any URL carrying `?user_view=1`, and left with `?user_view=0` or
from the mode flipper.

## The parameter

`user_view` is universal. `?user_view=1` renders a page in the user
view and `?user_view=0` leaves it; a request that does not state it
renders the page as it was. The context processor
(`monitor_app.context_processors.system_status_nav`) exposes
`user_view` (boolean) and `user_view_param` (`'1'`, `'0'` or empty) to
every template, so any page tailors itself with
`{% if not user_view %}` around what a collaborator does not need, and
a view reads `request.GET.get('user_view')` when it should not build
what it will not show.

No session survives the external proxy (EXTERNAL_ACCESS.md), so the
base template persists a stated value in the browser
(`localStorage.userView`, `data-user-view` on the document element).
The reduced nav therefore holds across links that carry no parameter,
while server-side tailoring follows the parameter alone; the user nav's
own links carry it, and a page's internal links should carry it where
the destination tailors itself.

## The nav

Contained simplicity. The brand at top left reads "epicprod User View"
and is a plain link to the user view home (`pcs/user/`, an empty page
whose content is not yet specified); it has no pulldown, and the mode
flipper is not shown. The menu is Requests (Request Production,
Production Requests), Campaign (Current campaign, Campaign Catalog),
Find data, then a wider gap and Full epicprod view, which leaves the
user view. The System menu and the built-at line are not shown; the
account name and Logout remain.

The conventions: white is for the title or a major section label,
everything else in the menu bar is the menu-bar blue (`#8fc7e8`), and
the user view has no sections, so its only white text is the brand.
`style.css` paints plain top-level links white; a link added to the
user nav takes the class `nav-user-item` for the blue.

## Pages

- Current campaign is the campaign plan (`pcs/plan/`) without the
  delivery map; the view does not build the snapper embed under
  `user_view=1`.
- The home page (`pcs/user/`) lives under `pcs/` so the external
  proxy's catch-all serves it without an swf-remote route.

Further tailoring is named page by page.
