"""SWF resolver mapping for Snapper event references.

The generic package returns event references naming stable resolver
identifiers (snapper-ai DESIGN.md, Event-reference contract); this
module is the SWF integration that maps those identifiers to concrete
REST routes and MCP tools and refines availability. Registered
identifiers stay stable; deployment URLs live only here.
"""

from django.urls import NoReverseMatch, reverse

# resolver identifier -> transport description. ``params`` documents the
# selector vocabulary a consumer may use against the target.
RESOLVER_MAP = {
    'swf-testbed-system-state-events': {
        'rest': 'monitor_app:systemstateevent-list',
        'rest_params': 'run_number, event_type, state',
        'mcp_tools': ['swf_get_system_state', 'swf_list_runs'],
        'notes': ('Exact E0-E1 datataking transitions; filter by the '
                  'run_number carried in the datataking lane hover.'),
        # A human reader lands on a page, never a REST document.
        'page': 'monitor_app:runs_list',
        'page_label': 'Runs and their transitions',
    },
    'swf-system-status-history': {
        'rest': 'monitor_app:system-status-history',
        'rest_params': 'name, start, end (ISO 8601), limit',
        'mcp_tools': [],
        'notes': ('Append-only health observations behind the assessed '
                  'health component.'),
        'page': 'monitor_app:system_status',
        'page_label': 'System health checks',
    },
    'swf-panda-activity-history': {
        'rest': 'monitor_app:panda-api-tasks-list',
        'rest_params': 'days, status, taskname (substring)',
        'mcp_tools': ['panda_list_jobs', 'panda_list_tasks',
                      'panda_error_summary', 'panda_study_job'],
        'notes': ('Authoritative PanDA job and task records; event time '
                  'is modificationtime.'),
        'page': 'monitor_app:panda_tasks_list',
        'page_label': 'PanDA tasks and jobs',
    },
}


def annotate_references(references):
    """Refine generic event references with concrete SWF transports.

    A mapped resolver becomes available with its REST URL and MCP tool
    names; an unmapped one stays unknown — never claimed available.
    """
    annotated = []
    for reference in references:
        entry = dict(reference)
        mapping = RESOLVER_MAP.get(entry.get('resolver'))
        if mapping:
            try:
                rest_url = reverse(mapping['rest'])
            except NoReverseMatch:
                rest_url = ''
            entry['availability'] = 'available' if rest_url else 'unknown'
            entry['transport'] = {
                'rest_url': rest_url,
                'rest_params': mapping['rest_params'],
                'mcp_tools': mapping['mcp_tools'],
                'notes': mapping['notes'],
            }
            if mapping.get('page'):
                try:
                    entry['page_url'] = reverse(mapping['page'])
                    entry['page_label'] = mapping.get(
                        'page_label') or entry.get('component', '')
                except NoReverseMatch:
                    pass
        annotated.append(entry)
    return annotated
