"""
PCS (Physics Configuration System) MCP tools — tag browsing and lookup.

Each tool registers with the MCP server and queries Django ORM via sync_to_async.
"""

from asgiref.sync import sync_to_async
from monitor_app.mcp import mcp


def _list_tags_sync(tag_type, category=None, status=None, creator=None,
                    search=None):
    """List tags with filtering. Returns list of tag summaries."""
    from pcs.schemas import TAG_SCHEMAS, get_tag_model

    if tag_type not in TAG_SCHEMAS:
        return {"error": f"Invalid tag_type '{tag_type}'. Use: p, e, s, r, k"}

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
    if not label or label[0] not in ('p', 'e', 's', 'r', 'k'):
        return {"error": f"Invalid tag label '{tag_label}'. Format: p1001, e3, s1, r1, k1"}

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

    types = [tag_type] if tag_type else ['p', 'e', 's', 'r', 'k']
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
        tag_type: Optional — restrict to one type: 'p', 'e', 's', 'r', 'k'.
                  If omitted, searches all tag types.

    Returns:
        query, count, and list of matching tags with: tag_label, status,
        description, parameters, category (physics only).
    """
    return await sync_to_async(_search_tags_sync)(
        query=query, tag_type=tag_type,
    )


# ---------------------------------------------------------------------------
# Datasets and Production Tasks — read + intake/lifecycle
#
# Write tools call ``pcs.services`` so REST viewset actions and MCP tools
# share validation, idempotency, and lifecycle rules.
# ---------------------------------------------------------------------------

def _dataset_to_dict(ds, full=True):
    out = {
        'did': ds.did,
        'dataset_name': ds.dataset_name,
        'scope': ds.scope,
        'detector_version': ds.detector_version,
        'detector_config': ds.detector_config,
        'stage': ds.stage,
        'is_external': ds.is_external,
        'source_kind': ds.source_kind,
        'source_location': ds.source_location,
    }
    if full:
        out.update({
            'physics_tag': ds.physics_tag.tag_label,
            'evgen_tag': ds.evgen_tag.tag_label,
            'simu_tag': ds.simu_tag.tag_label,
            'reco_tag': ds.reco_tag.tag_label,
            'background_tag': ds.background_tag.tag_label if ds.background_tag_id else None,
            'block_num': ds.block_num,
            'blocks': ds.blocks,
            'description': ds.description,
            'metadata': ds.metadata,
            'created_by': ds.created_by,
            'created_at': ds.created_at.isoformat() if ds.created_at else None,
        })
    return out


def _prodtask_to_dict(t, full=True):
    out = {
        'name': t.name,
        'status': t.status,
        'panda_task_id': t.panda_task_id,
        'output_dataset_dids': [d.did for d in t.output_datasets],
        'input_dataset_dids':  [d.did for d in t.input_datasets],
        'intermediate_dataset_dids': [d.did for d in t.intermediate_datasets],
        'input_source_kind': t.input_source_kind,
        'input_source_location': t.input_source_location,
        'input_source_stage': t.input_source_stage,
    }
    if full:
        out.update({
            'description': t.description,
            'prod_config': t.prod_config.name,
            'overrides': t.overrides or {},
            'csv_file': t.csv_file,
            'condor_cluster_id': t.condor_cluster_id,
            'created_by': t.created_by,
            'created_at': t.created_at.isoformat() if t.created_at else None,
            'updated_at': t.updated_at.isoformat() if t.updated_at else None,
        })
    return out


# ---- Dataset read ----------------------------------------------------------

def _dataset_list_sync(stage=None, source_kind=None, source_location=None,
                       scope=None, name_contains=None, limit=20, offset=0):
    from pcs.models import Dataset
    qs = Dataset.objects.select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag'
    ).order_by('-created_at')
    if scope:
        qs = qs.filter(scope=scope)
    if name_contains:
        qs = qs.filter(dataset_name__icontains=name_contains)
    # stage / source_kind / source_location are derived from metadata JSON
    if stage:
        qs = qs.filter(metadata__stage=stage)
    if source_kind:
        qs = qs.filter(metadata__source__kind=source_kind)
    if source_location:
        qs = qs.filter(metadata__source__location=source_location)
    total = qs.count()
    items = [_dataset_to_dict(d, full=False) for d in qs[offset:offset + limit]]
    return {'count': total, 'limit': limit, 'offset': offset, 'datasets': items}


def _dataset_get_sync(did=None, dataset_name=None):
    from pcs.models import Dataset
    if not (did or dataset_name):
        return {'error': 'Provide did or dataset_name'}
    qs = Dataset.objects.select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag'
    )
    ds = (qs.filter(did=did).first() if did
          else qs.filter(dataset_name=dataset_name).first())
    if not ds:
        return {'error': f'Dataset not found: {did or dataset_name}'}
    return _dataset_to_dict(ds, full=True)


@mcp.tool()
async def pcs_dataset_list(
    stage: str = None,
    source_kind: str = None,
    source_location: str = None,
    scope: str = None,
    name_contains: str = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """
    List PCS Datasets with optional filtering.

    Filters cover the dataset metadata model — `stage` (e.g. 'evgen'),
    external `source_kind` (e.g. 'csv_manifest'), exact `source_location`,
    and name/scope. Use `name_contains` for substring match.

    Returns:
        count, limit, offset, and a list of dataset summaries (DID,
        dataset_name, scope, detector, stage, source_kind, source_location).
    """
    return await sync_to_async(_dataset_list_sync)(
        stage=stage, source_kind=source_kind,
        source_location=source_location, scope=scope,
        name_contains=name_contains, limit=limit, offset=offset,
    )


@mcp.tool()
async def pcs_dataset_get(did: str = None, dataset_name: str = None) -> dict:
    """
    Get full details of a single Dataset by DID or dataset_name.

    Args:
        did: Rucio-style DID, e.g. 'group.EIC:....b1'.
        dataset_name: Dataset name without scope/block suffix.

    Returns: full dataset record (tags, blocks, metadata).
    """
    return await sync_to_async(_dataset_get_sync)(
        did=did, dataset_name=dataset_name,
    )


# ---- Dataset write (intake) ------------------------------------------------

def _dataset_intake_sync(*, source_location, source_kind, scope, stage,
                         detector_version, detector_config,
                         physics_tag, evgen_tag, simu_tag, reco_tag,
                         background_tag,
                         description, created_by):
    from pcs import services
    try:
        ds, created = services.dataset_intake(
            source_location=source_location, source_kind=source_kind,
            scope=scope, stage=stage,
            detector_version=detector_version, detector_config=detector_config,
            physics_tag_label=physics_tag, evgen_tag_label=evgen_tag,
            simu_tag_label=simu_tag,       reco_tag_label=reco_tag,
            background_tag_label=background_tag,
            description=description, created_by=created_by,
        )
    except services.ServiceError as e:
        return {'error': e.detail}
    return {'created': created, 'dataset': _dataset_to_dict(ds, full=True)}


@mcp.tool()
async def pcs_dataset_intake(
    source_location: str,
    source_kind: str = 'csv_manifest',
    physics_tag: str = None,
    evgen_tag: str = None,
    simu_tag: str = None,
    reco_tag: str = None,
    background_tag: str = None,
    detector_version: str = None,
    detector_config: str = None,
    scope: str = 'group.EIC.evgen',
    stage: str = 'evgen',
    description: str = '',
    created_by: str = 'mcp',
) -> dict:
    """
    Idempotent intake of an external (e.g. EVGEN CSV manifest) Dataset.

    Idempotency key: (source_kind, source_location). If a Dataset already
    records the same source, returns it unchanged. Otherwise creates a new
    Dataset (requires the four locked tag labels and detector handles) with
    metadata.stage and metadata.source.{kind,location} set.

    Returns: {created: bool, dataset: {...}}.
    """
    return await sync_to_async(_dataset_intake_sync)(
        source_location=source_location, source_kind=source_kind,
        scope=scope, stage=stage,
        detector_version=detector_version, detector_config=detector_config,
        physics_tag=physics_tag, evgen_tag=evgen_tag,
        simu_tag=simu_tag, reco_tag=reco_tag,
        background_tag=background_tag,
        description=description, created_by=created_by,
    )


# ---- ProdTask read ---------------------------------------------------------

def _prodtask_list_sync(status=None, public_catalog_issue=None,
                        name_contains=None, limit=20, offset=0):
    from pcs.models import ProdTask
    qs = ProdTask.objects.select_related(
        'dataset', 'prod_config'
    ).order_by('-updated_at')
    if status:
        qs = qs.filter(status=status)
    if public_catalog_issue is not None:
        qs = qs.filter(overrides__public_catalog_issue=public_catalog_issue)
    if name_contains:
        qs = qs.filter(name__icontains=name_contains)
    total = qs.count()
    items = [_prodtask_to_dict(t, full=False) for t in qs[offset:offset + limit]]
    return {'count': total, 'limit': limit, 'offset': offset, 'tasks': items}


def _prodtask_get_sync(name):
    from pcs.models import ProdTask
    t = (ProdTask.objects.select_related('dataset', 'prod_config')
                          .filter(name=name).first())
    if not t:
        return {'error': f'Task not found: {name}'}
    return _prodtask_to_dict(t, full=True)


def _prodtask_artifact_sync(name, fmt):
    from pcs.models import ProdTask
    from pcs.commands import (
        build_condor_command, build_panda_command,
        build_task_params, build_task_dump,
    )
    if fmt not in ('condor', 'panda', 'jedi', 'dump'):
        return {'error': "fmt must be one of: condor, panda, jedi, dump"}
    t = ProdTask.objects.select_related('dataset', 'prod_config').filter(name=name).first()
    if not t:
        return {'error': f'Task not found: {name}'}
    if fmt == 'condor':
        return {'name': name, 'fmt': 'condor', 'value': build_condor_command(t)}
    if fmt == 'panda':
        return {'name': name, 'fmt': 'panda', 'value': build_panda_command(t)}
    if fmt == 'jedi':
        return {'name': name, 'fmt': 'jedi', 'taskParamMap': build_task_params(t)}
    return {'name': name, 'fmt': 'dump', 'dump': build_task_dump(t)}


@mcp.tool()
async def pcs_prodtask_list(
    status: str = None,
    public_catalog_issue: int = None,
    name_contains: str = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """
    List PCS ProdTasks with optional filtering.

    Filters: lifecycle `status` (draft/ready/submitted/completed/failed),
    `public_catalog_issue` (GitHub issue number on epic-prod), or
    `name_contains` (substring).

    Returns:
        count, limit, offset, and task summaries (name, status,
        panda_task_id, output/input/intermediate DIDs, input_source_*).
    """
    return await sync_to_async(_prodtask_list_sync)(
        status=status, public_catalog_issue=public_catalog_issue,
        name_contains=name_contains, limit=limit, offset=offset,
    )


@mcp.tool()
async def pcs_prodtask_get(name: str) -> dict:
    """
    Get full details of a single ProdTask by name.

    Returns: name, status, panda_task_id, prod_config, description,
    overrides, csv_file (legacy), the three dataset DID lists, and the
    derived input_source_{kind,location,stage}.
    """
    return await sync_to_async(_prodtask_get_sync)(name=name)


@mcp.tool()
async def pcs_prodtask_artifact(name: str, fmt: str = 'dump') -> dict:
    """
    Get a ProdTask submission artifact regenerated from current PCS state.

    Args:
        name: ProdTask.name.
        fmt:  'condor' | 'panda' | 'jedi' | 'dump' (default 'dump').

    Returns: a dict with the requested artifact.
        - condor / panda: {value: <command string>}
        - jedi:           {taskParamMap: {...}}  (passable to insertTaskParams)
        - dump:           {dump: {task, dataset, tags, prod_config, effective_config}}
    """
    return await sync_to_async(_prodtask_artifact_sync)(name=name, fmt=fmt)


# ---- ProdTask write --------------------------------------------------------

def _prodtask_intake_sync(payload, created_by):
    from pcs import services
    try:
        task, created = services.prodtask_intake(
            payload=payload, created_by=created_by,
        )
    except services.ServiceError as e:
        return {'error': e.detail}
    return {'created': created, 'task': _prodtask_to_dict(task, full=True)}


def _prodtask_link_input_sync(task_name, did=None, dids=None):
    from pcs.models import ProdTask
    from pcs import services
    task = ProdTask.objects.select_related('dataset', 'prod_config').filter(name=task_name).first()
    if not task:
        return {'error': f'Task not found: {task_name}'}
    try:
        services.prodtask_link_input(task=task, did=did, dids=dids)
    except services.ServiceError as e:
        return {'error': e.detail}
    return {'task': _prodtask_to_dict(task, full=True)}


def _prodtask_set_status_sync(task_name, new_status):
    from pcs.models import ProdTask
    from pcs import services
    task = ProdTask.objects.select_related('dataset', 'prod_config').filter(name=task_name).first()
    if not task:
        return {'error': f'Task not found: {task_name}'}
    try:
        services.prodtask_set_status(task=task, new_status=new_status)
    except services.ServiceError as e:
        return {'error': e.detail}
    return {'task': _prodtask_to_dict(task, full=True)}


@mcp.tool()
async def pcs_prodtask_intake(
    public_catalog_issue: int = None,
    public_catalog_csv_path: str = None,
    public_catalog_row_key: str = None,
    name: str = None,
    dataset: str = None,
    prod_config: str = None,
    description: str = None,
    input_dataset_did: str = None,
    public_catalog_repo: str = None,
    public_catalog_pr: int = None,
    public_catalog_row_index: int = None,
    public_catalog_page_url: str = None,
    public_catalog_commit_sha: str = None,
    created_by: str = 'mcp',
) -> dict:
    """
    Idempotent intake of a draft ProdTask.

    Idempotency key (one required): public_catalog_issue (preferred)
    OR (public_catalog_csv_path, public_catalog_row_key). On match, the
    existing task's catalogue mapping is merged into overrides and
    description / input_dataset_did are optionally updated. On no match,
    creates a draft requiring `name`, `dataset` (DID or name), and
    `prod_config` (name).

    Returns: {created: bool, task: {...}}.
    """
    payload = {k: v for k, v in {
        'public_catalog_issue': public_catalog_issue,
        'public_catalog_csv_path': public_catalog_csv_path,
        'public_catalog_row_key': public_catalog_row_key,
        'name': name, 'dataset': dataset, 'prod_config': prod_config,
        'description': description, 'input_dataset_did': input_dataset_did,
        'public_catalog_repo': public_catalog_repo,
        'public_catalog_pr': public_catalog_pr,
        'public_catalog_row_index': public_catalog_row_index,
        'public_catalog_page_url': public_catalog_page_url,
        'public_catalog_commit_sha': public_catalog_commit_sha,
    }.items() if v is not None}
    return await sync_to_async(_prodtask_intake_sync)(
        payload=payload, created_by=created_by,
    )


@mcp.tool()
async def pcs_prodtask_link_input(
    task_name: str,
    did: str = None,
    dids: list = None,
) -> dict:
    """
    Link input Dataset(s) to a ProdTask via overrides JSON.

    Provide one of `did` (single DID) or `dids` (list of DIDs), not both.
    Linked Datasets must already exist; this never creates Datasets.

    Returns: {task: {...}}.
    """
    return await sync_to_async(_prodtask_link_input_sync)(
        task_name=task_name, did=did, dids=dids,
    )


@mcp.tool()
async def pcs_prodtask_set_status(task_name: str, status: str) -> dict:
    """
    Transition a ProdTask to a new lifecycle state.

    Allowed transitions:
        draft     -> ready
        ready     -> {draft, submitted}
        submitted -> {completed, failed}
        completed/failed: terminal.

    Note: submitted is normally written by a JEDI submission flow
    (record-submission), not directly through this tool. Submission
    itself is not exposed via MCP — operators run `pcs-task-cmd <name>
    --submit` from a host with a valid PanDA auth context (proxy or
    OIDC token).

    Returns: {task: {...}}.
    """
    return await sync_to_async(_prodtask_set_status_sync)(
        task_name=task_name, new_status=status,
    )
