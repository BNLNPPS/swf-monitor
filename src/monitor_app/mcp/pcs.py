"""
PCS (Physics Configuration System) MCP tools — tag browsing and lookup.

Each tool registers with the MCP server and queries Django ORM via sync_to_async.
"""

from asgiref.sync import sync_to_async
from mcp_server import mcp_server as mcp


def _list_tags_sync(tag_type, category=None, status=None, creator=None,
                    search=None):
    """List tags with filtering. Returns list of tag summaries."""
    from pcs.schemas import TAG_SCHEMAS, get_tag_model

    if tag_type not in TAG_SCHEMAS:
        return {"error": f"Invalid tag_type '{tag_type}'. Use: p, e, s, r"}

    model = get_tag_model(tag_type)
    qs = model.objects.order_by('-tag_number')

    if tag_type == 'p':
        qs = qs.select_related('category')
        if category:
            qs = qs.filter(category__name__iexact=category)

    if status:
        qs = qs.filter(status=status.lower())
    if creator:
        qs = qs.filter(created_by=creator)

    if search:
        from django.db.models import Q
        q = Q(description__icontains=search) | Q(tag_label__icontains=search)
        qs = qs.filter(q)

    tags = []
    for t in qs:
        entry = {
            'tag_label': t.tag_label,
            'status': t.status,
            'description': t.description,
            'created_by': t.created_by,
            'parameters': t.parameters,
        }
        if tag_type == 'p':
            entry['category'] = t.category.name
        tags.append(entry)

    schema = TAG_SCHEMAS[tag_type]
    return {
        'tag_type': tag_type,
        'label': schema['label'],
        'count': len(tags),
        'tags': tags,
    }


def _get_tag_sync(tag_label):
    """Get a single tag by label (e.g. 'p1001', 'e3', 'r1')."""
    label = tag_label.strip().lower()
    if not label or label[0] not in ('p', 'e', 's', 'r'):
        return {"error": f"Invalid tag label '{tag_label}'. Format: p1001, e3, s1, r1"}

    prefix = label[0]
    try:
        number = int(label[1:])
    except ValueError:
        return {"error": f"Invalid tag number in '{tag_label}'"}

    from pcs.schemas import get_tag_model
    model = get_tag_model(prefix)

    try:
        t = model.objects.get(tag_number=number)
    except model.DoesNotExist:
        return {"error": f"Tag {tag_label} not found"}

    result = {
        'tag_label': t.tag_label,
        'tag_number': t.tag_number,
        'status': t.status,
        'description': t.description,
        'parameters': t.parameters,
        'created_by': t.created_by,
        'created_at': t.created_at.isoformat(),
    }
    if prefix == 'p':
        t_with_cat = model.objects.select_related('category').get(tag_number=number)
        result['category'] = t_with_cat.category.name
        result['category_digit'] = t_with_cat.category.digit

    return result


def _search_tags_sync(query, tag_type=None):
    """Search across tag descriptions and parameters."""
    from pcs.schemas import TAG_SCHEMAS, get_tag_model

    types = [tag_type] if tag_type else ['p', 'e', 's', 'r']
    results = []

    for tt in types:
        if tt not in TAG_SCHEMAS:
            continue
        model = get_tag_model(tt)
        qs = model.objects.order_by('-tag_number')
        if tt == 'p':
            qs = qs.select_related('category')

        q_lower = query.lower()
        for t in qs:
            searchable = ' '.join([
                t.tag_label, t.description,
                ' '.join(str(v) for v in t.parameters.values()),
            ]).lower()
            if q_lower in searchable:
                entry = {
                    'tag_label': t.tag_label,
                    'status': t.status,
                    'description': t.description,
                    'parameters': t.parameters,
                }
                if tt == 'p':
                    entry['category'] = t.category.name
                results.append(entry)

    return {
        'query': query,
        'count': len(results),
        'tags': results,
    }


@mcp.tool()
async def pcs_list_tags(
    tag_type: str,
    category: str = None,
    status: str = None,
    creator: str = None,
    search: str = None,
) -> dict:
    """
    List PCS tags (production task configurations) with optional filtering.

    PCS tags capture physics process, event generation, simulation, and
    reconstruction configurations for ePIC Monte Carlo production campaigns.

    Args:
        tag_type: Tag type — 'p' (physics), 'e' (evgen), 's' (simu), 'r' (reco). Required.
        category: Physics tags only — filter by category name (e.g. 'DIS', 'DVCS', 'EXCLUSIVE').
        status: Filter by status: 'draft' or 'locked'.
        creator: Filter by creator username.
        search: Text search in tag label and description.

    Returns:
        tag_type, label, count, and list of tags with: tag_label, status,
        description, parameters, created_by, category (physics only).
    """
    return await sync_to_async(_list_tags_sync)(
        tag_type=tag_type, category=category, status=status,
        creator=creator, search=search,
    )


@mcp.tool()
async def pcs_get_tag(tag_label: str) -> dict:
    """
    Get full details of a single PCS tag by its label.

    Args:
        tag_label: The tag label, e.g. 'p1001', 'e3', 's1', 'r1'.
                   Case-insensitive.

    Returns:
        tag_label, tag_number, status, description, parameters (all key-value
        pairs), created_by, created_at, and category/category_digit for physics tags.
    """
    return await sync_to_async(_get_tag_sync)(tag_label=tag_label)


@mcp.tool()
async def pcs_search_tags(
    query: str,
    tag_type: str = None,
) -> dict:
    """
    Search across PCS tags by text in label, description, or parameter values.

    Use this when you don't know the exact tag label but know a keyword like
    'photoproduction', 'pythia8', 'eAu', or 'minQ2=1000'.

    Args:
        query: Search text (case-insensitive). Matches against tag label,
               description, and all parameter values.
        tag_type: Optional — restrict to one type: 'p', 'e', 's', 'r'.
                  If omitted, searches all tag types.

    Returns:
        query, count, and list of matching tags with: tag_label, status,
        description, parameters, category (physics only).
    """
    return await sync_to_async(_search_tags_sync)(
        query=query, tag_type=tag_type,
    )
