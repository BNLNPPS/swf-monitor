"""
PCS business logic — single source of truth for intake, linkage,
lifecycle, and submission state changes.

Both REST viewset actions and MCP tools call these functions. Functions
take plain Python types (no HTTP request, no MCP context) and return
model instances or raise ServiceError. The caller translates errors
to its native shape (DRF Response, MCP error dict).
"""
from .models import (
    Dataset, ProdConfig, ProdTask,
    PhysicsTag, EvgenTag, SimuTag, RecoTag,
)


class ServiceError(Exception):
    """Domain error with an HTTP-shaped status hint and detail message."""
    def __init__(self, detail, status=400):
        self.detail = detail
        self.status = status
        super().__init__(detail)


# Allowed ProdTask lifecycle transitions. Submission and post-submission
# state changes are recorded via prodtask_record_submission and
# automation, not direct human transitions.
PRODTASK_TRANSITIONS = {
    'draft':     {'ready'},
    'ready':     {'draft', 'submitted'},
    'submitted': {'completed', 'failed'},
    'completed': set(),
    'failed':    set(),
}

PUBLIC_CATALOG_KEYS = (
    'public_catalog_repo', 'public_catalog_issue', 'public_catalog_pr',
    'public_catalog_row_index', 'public_catalog_csv_path',
    'public_catalog_row_key', 'public_catalog_page_url',
    'public_catalog_commit_sha',
)


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def dataset_intake(*, source_location, source_kind='csv_manifest',
                   scope='group.EIC.evgen', stage='evgen',
                   detector_version=None, detector_config=None,
                   physics_tag_label=None, evgen_tag_label=None,
                   simu_tag_label=None, reco_tag_label=None,
                   description='', created_by):
    """
    Idempotent intake of an external (e.g. EVGEN CSV manifest) Dataset.

    Idempotency key: ``(source.kind, source.location)``.

    Returns ``(dataset, was_created)``.
    """
    if not source_location:
        raise ServiceError('source_location is required')

    existing = Dataset.objects.filter(
        metadata__source__location=source_location,
        metadata__source__kind=source_kind,
    ).first()
    if existing:
        return existing, False

    required = {
        'detector_version': detector_version,
        'detector_config':  detector_config,
        'physics_tag':      physics_tag_label,
        'evgen_tag':        evgen_tag_label,
        'simu_tag':         simu_tag_label,
        'reco_tag':         reco_tag_label,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ServiceError(
            f'No existing Dataset for {source_kind}:{source_location}; '
            f'creation requires: {", ".join(missing)}'
        )

    tag_specs = {
        'physics_tag': (PhysicsTag, physics_tag_label),
        'evgen_tag':   (EvgenTag,   evgen_tag_label),
        'simu_tag':    (SimuTag,    simu_tag_label),
        'reco_tag':    (RecoTag,    reco_tag_label),
    }
    tags = {}
    for field, (model, label) in tag_specs.items():
        tag = model.objects.filter(tag_label=label).first()
        if not tag:
            raise ServiceError(f'{field} not found: {label}')
        if tag.status != 'locked':
            raise ServiceError(f'{field} {label} must be locked before use')
        tags[field] = tag

    ds = Dataset(
        scope=scope,
        detector_version=detector_version,
        detector_config=detector_config,
        description=description,
        metadata={
            'stage': stage,
            'source': {'kind': source_kind, 'location': source_location},
        },
        created_by=created_by,
        **tags,
    )
    try:
        ds.save()
    except Exception as e:
        raise ServiceError(str(e))
    return ds, True


# ---------------------------------------------------------------------------
# ProdTask
# ---------------------------------------------------------------------------

def prodtask_intake(*, payload, created_by):
    """
    Idempotent intake of a draft ProdTask.

    Idempotency key (one of, in payload):
        public_catalog_issue (preferred), or
        (public_catalog_csv_path, public_catalog_row_key)

    On match the existing task is updated (catalogue mapping merged
    into overrides; description optionally refreshed; input_dataset_did
    optionally set/updated). On no match, a draft task is created and
    requires ``name``, ``dataset`` (DID or name), and ``prod_config``
    (name) in the payload.

    Returns ``(task, was_created)``.
    """
    key_issue = payload.get('public_catalog_issue')
    key_csv_path = payload.get('public_catalog_csv_path')
    key_row = payload.get('public_catalog_row_key')
    if not key_issue and not (key_csv_path and key_row):
        raise ServiceError(
            'Idempotency key required: public_catalog_issue or '
            '(public_catalog_csv_path, public_catalog_row_key)'
        )

    if key_issue:
        existing = ProdTask.objects.filter(
            overrides__public_catalog_issue=key_issue
        ).first()
    else:
        existing = ProdTask.objects.filter(
            overrides__public_catalog_csv_path=key_csv_path,
            overrides__public_catalog_row_key=key_row,
        ).first()

    new_catalog = {k: payload[k] for k in PUBLIC_CATALOG_KEYS if k in payload}
    new_input_did = payload.get('input_dataset_did')

    if existing:
        ov = dict(existing.overrides or {})
        ov.update(new_catalog)
        if new_input_did:
            ov['input_dataset_did'] = new_input_did
        existing.overrides = ov
        update_fields = ['overrides', 'updated_at']
        if payload.get('description') is not None:
            existing.description = payload['description']
            update_fields.append('description')
        existing.save(update_fields=update_fields)
        return existing, False

    name = payload.get('name')
    dataset_handle = payload.get('dataset')
    config_handle = payload.get('prod_config')
    missing = [k for k, v in [('name', name),
                              ('dataset', dataset_handle),
                              ('prod_config', config_handle)]
               if not v]
    if missing:
        raise ServiceError(
            f'Creating a new task requires: {", ".join(missing)}'
        )
    output_ds = (Dataset.objects.filter(did=dataset_handle).first()
                 or Dataset.objects.filter(dataset_name=dataset_handle).first())
    if not output_ds:
        raise ServiceError(f'Output dataset not found: {dataset_handle}')
    config = ProdConfig.objects.filter(name=config_handle).first()
    if not config:
        raise ServiceError(f'ProdConfig not found: {config_handle}')

    ov = dict(new_catalog)
    if new_input_did:
        ov['input_dataset_did'] = new_input_did
    task = ProdTask.objects.create(
        name=name,
        description=payload.get('description', ''),
        status='draft',
        dataset=output_ds,
        prod_config=config,
        overrides=ov or None,
        created_by=created_by,
    )
    return task, True


def prodtask_link_input(*, task, did=None, dids=None):
    """
    Link input Dataset(s) to a ProdTask via overrides JSON. Provide
    one of ``did`` (single) or ``dids`` (list), not both. Linked
    Datasets must already exist; this never creates Datasets.
    """
    if did and dids:
        raise ServiceError('Provide one of did or dids, not both')
    if not did and not dids:
        raise ServiceError('did or dids is required')
    targets = [did] if did else list(dids)
    found = set(Dataset.objects.filter(did__in=targets)
                .values_list('did', flat=True))
    missing = [d for d in targets if d not in found]
    if missing:
        raise ServiceError(f'Dataset(s) not found: {missing}')
    ov = dict(task.overrides or {})
    if did:
        ov['input_dataset_did'] = did
        ov.pop('input_dataset_dids', None)
    else:
        ov['input_dataset_dids'] = list(dids)
        ov.pop('input_dataset_did', None)
    task.overrides = ov
    task.save(update_fields=['overrides', 'updated_at'])
    return task


def prodtask_set_status(*, task, new_status):
    """Lifecycle transition with rule enforcement."""
    valid = [c[0] for c in task._meta.get_field('status').choices]
    if new_status not in valid:
        raise ServiceError(
            f'Invalid status. Choose from: {", ".join(valid)}'
        )
    allowed = PRODTASK_TRANSITIONS.get(task.status, set())
    if new_status != task.status and new_status not in allowed:
        raise ServiceError(
            f'Cannot transition from {task.status!r} to {new_status!r}. '
            f'Allowed from {task.status!r}: '
            f'{sorted(allowed) or "(terminal)"}'
        )
    task.status = new_status
    task.save(update_fields=['status', 'updated_at'])
    return task


def prodtask_record_submission(*, task, jedi_task_id, new_status='submitted'):
    """
    Record outcome of a JEDI submission. Two gates:

    - ``task.status`` must be 'ready' (no submit from draft, no re-submit).
    - ``task.panda_task_id`` must be unset; refuses to overwrite (409).
    """
    if task.status != 'ready':
        raise ServiceError(
            f'Task must be in status=ready before submission '
            f'(current: {task.status!r}). Mark it ready via '
            f'set-status first.'
        )
    if task.panda_task_id is not None:
        raise ServiceError(
            f'Task already records panda_task_id={task.panda_task_id}. '
            f'Refusing to overwrite.',
            status=409,
        )
    try:
        task.panda_task_id = int(jedi_task_id)
    except (TypeError, ValueError):
        raise ServiceError('jedi_task_id must be an integer')

    valid = [c[0] for c in task._meta.get_field('status').choices]
    if new_status not in valid:
        raise ServiceError(
            f'Invalid status. Choose from: {", ".join(valid)}'
        )
    task.status = new_status
    task.save(update_fields=['panda_task_id', 'status', 'updated_at'])
    return task
