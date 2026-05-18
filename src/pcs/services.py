"""
PCS business logic — single source of truth for intake, linkage,
lifecycle, and submission state changes.

Both REST viewset actions and MCP tools call these functions. Functions
take plain Python types (no HTTP request, no MCP context) and return
model instances or raise ServiceError. The caller translates errors
to its native shape (DRF Response, MCP error dict).
"""
import csv as _csv
import hashlib as _hashlib
import logging as _logging
import re as _re

from django.db import transaction

_log = _logging.getLogger(__name__)

from .models import (
    Dataset, ProdConfig, ProdTask,
    Campaign, ProdRequest,
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

# Fields copied from ProdRequest → ProdTask at task creation. Both rows
# are independently mutable thereafter. Per
# memory:feedback-denormalization-ok — the duplication is intentional so
# catalog filter/display reads direct ProdTask columns without joining.
REQUEST_TO_TASK_COPY_FIELDS = (
    'requestor', 'priority',
    'pre_tdr_use', 'early_science_use', 'other_use', 'new_request',
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


def _known_prodtask_statuses():
    """Universe of legal ProdTask status values, derived from the
    transition map rather than a CharField choices enum."""
    known = set(PRODTASK_TRANSITIONS.keys())
    for trans in PRODTASK_TRANSITIONS.values():
        known.update(trans)
    return known


def prodtask_set_status(*, task, new_status):
    """Lifecycle transition with rule enforcement."""
    valid = _known_prodtask_statuses()
    if new_status not in valid:
        raise ServiceError(
            f'Invalid status. Choose from: {", ".join(sorted(valid))}'
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


def prodtask_apply_request(task, request, *, save=True):
    """Copy seed fields from ``request`` onto ``task`` (request → task).

    Mutates ``task.request`` and the fields listed in
    ``REQUEST_TO_TASK_COPY_FIELDS``. Saves by default. After this call
    both rows are independently mutable; a later request edit does
    *not* automatically resync to the task. Call this again explicitly
    if a resync is desired.
    """
    if request is None:
        raise ServiceError('request is required')
    task.request = request
    for field in REQUEST_TO_TASK_COPY_FIELDS:
        setattr(task, field, getattr(request, field))
    if save:
        task.save(update_fields=['request'] + list(REQUEST_TO_TASK_COPY_FIELDS)
                                + ['updated_at'])
    return task


# ---------------------------------------------------------------------------
# Campaign
# ---------------------------------------------------------------------------

def campaign_set_current(campaign):
    """Atomically promote ``campaign`` to lifecycle='current'.

    Any other Campaign currently at 'current' is demoted to 'past'.
    No-op if ``campaign`` is already current. Service-layer enforcement
    (no DB constraint) — direct admin saves can violate the invariant.
    """
    with transaction.atomic():
        if campaign.lifecycle == 'current':
            return campaign
        Campaign.objects.filter(lifecycle='current').exclude(pk=campaign.pk) \
                        .update(lifecycle='past')
        campaign.lifecycle = 'current'
        campaign.save(update_fields=['lifecycle', 'updated_at'])
    return campaign


def campaign_clone_to_new(source, *, name, created_by,
                          description='', lifecycle='future'):
    """Create a new Campaign whose ``clone_of`` points to ``source``.

    The new campaign is blank — tasks are not cloned by this helper.
    Task cloning is a separate operation (potentially per-task, as
    dataset rebinding for the new campaign's detector_version is
    non-trivial). Use ``campaign_set_current`` separately to promote.
    """
    if Campaign.objects.filter(name=name).exists():
        raise ServiceError(f'Campaign name already in use: {name}')
    if lifecycle not in {'past', 'current', 'future'}:
        raise ServiceError(f'Invalid lifecycle: {lifecycle!r}')
    new_c = Campaign.objects.create(
        name=name,
        lifecycle=lifecycle,
        description=description,
        clone_of=source,
        created_by=created_by,
    )
    return new_c


# ---------------------------------------------------------------------------
# Submission state changes
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Default-datasets CSV import (Sakib's epic-prod docs/_data/datasets.csv)
# ---------------------------------------------------------------------------

# Local path to the cloned epic-prod CSV on swf-testbed.
DEFAULT_DATASETS_CSV_PATH = (
    '/data/wenauseic/github/epic-prod/docs/_data/datasets.csv'
)


_YESNO_RECOGNISED = {'', 'yes', 'y', 'no', 'n', 'true', 'false', '0', '1'}


def _yesno(value):
    """Parse Sakib's Yes/No into a boolean; everything else -> False.

    Logs a WARN when a non-empty value is unrecognised (e.g. 'Maybe'),
    so silent drops surface in the log. NO-SILENT-FAILURES rule.
    """
    s = str(value or '').strip().lower()
    if s and s not in _YESNO_RECOGNISED:
        _log.warning('_yesno: unrecognised value %r treated as False', value)
    return s in ('yes', 'y', 'true', '1')


_GH_EIC_RELEASE_RE = _re.compile(r'https?://github\.com/eic/([^/]+)/releases\b', _re.I)
_GH_OTHER_RELEASE_RE = _re.compile(r'https?://github\.com/([^/]+)/([^/]+)/releases\b', _re.I)
_GL_EIC_RE   = _re.compile(r'https?://gitlab\.com/eic/([^?#]+?)/-/', _re.I)
_GL_OTHER_RE = _re.compile(r'https?://gitlab\.com/([^?#]+?)/-/', _re.I)

# Owner / group prefixes to strip from the extracted evgen identifier.
# They're consistent organisational namespaces, not generator names, so
# leaving them in just makes the filter values noisy and prevents merging
# of variants. Add new prefixes here as new sources appear.
_EVGEN_STRIP_PREFIXES = ('JeffersonLab/', 'mceg/')


def _extract_evgen(value):
    """Derive a short evgen identifier from a Generator/Dataset Version cell.

    - github.com/eic/<repo>/releases/...            -> '<repo>'
    - github.com/<owner>/<repo>/releases/... (non-eic) -> '<owner>/<repo>'
    - gitlab.com/eic/<path>/-/...                   -> '<path>'  (collapses
                                                       tag variants of the
                                                       same project under
                                                       one filter value)
    - gitlab.com/<path>/-/... (non-eic)             -> '<path>'
    - plain strings ('starlight', 'Pythia 8', ...)  -> unchanged

    Stored at ingest in overrides.csv_import.evgen so the catalog can
    filter and group by generator without re-parsing per render. The
    catalog will eventually bind these to canonical EvgenTag entries.
    """
    s = str(value or '').strip()
    if not s:
        return ''
    out = s
    m = _GH_EIC_RELEASE_RE.match(s)
    if m:
        out = m.group(1)
    elif (m := _GH_OTHER_RELEASE_RE.match(s)):
        out = f'{m.group(1)}/{m.group(2)}'
    elif (m := _GL_EIC_RE.match(s)):
        out = m.group(1)
    elif (m := _GL_OTHER_RE.match(s)):
        out = m.group(1)
    for prefix in _EVGEN_STRIP_PREFIXES:
        if out.startswith(prefix):
            return out[len(prefix):]
    return out


def _parse_event_count(value):
    """Parse Sakib's event-count strings ('400k', '1M', '2.75M', plain
    integers) into an absolute event count.

    Returns None for empty input (legitimate); WARN-logs and returns
    None for non-empty unparseable input. NO-SILENT-FAILURES rule —
    the previous strict int() parse dropped every populated row in
    the CSV without surfacing it anywhere.
    """
    raw = value
    s = str(value or '').strip().replace(',', '').replace(' ', '')
    if not s:
        return None
    mult = 1
    if s[-1] in ('k', 'K'):
        mult, s = 1_000, s[:-1]
    elif s[-1] in ('m', 'M'):
        mult, s = 1_000_000, s[:-1]
    elif s[-1] in ('b', 'B', 'g', 'G'):
        mult, s = 1_000_000_000, s[:-1]
    try:
        return int(round(float(s) * mult))
    except (TypeError, ValueError):
        _log.warning('_parse_event_count: unparseable value %r', raw)
        return None


def _possible(value):
    """Truthy unless explicitly negative — 'Maybe' counts as yes.
    Used for pTDR ('possible pre-TDR use') and Other Use, where any
    non-empty, non-No marker indicates the use applies."""
    s = str(value or '').strip().lower()
    return bool(s) and s not in ('no', 'n', 'false', '0')


def _csvimport_slug(dataset_path, gen_version):
    """Short stable slug derived from the (path, generator) idempotency key."""
    key = f'{dataset_path}|{gen_version}'.encode()
    return _hashlib.sha1(key).hexdigest()[:12]


def _ensure_csvimport_anchors():
    """Resolve the placeholder Dataset-FK targets used by CSV-imported rows.

    Returns ``(physics_tag, evgen_tag, simu_tag, reco_tag, prod_config,
    campaign)``. All must already exist; this is a lookup, not a
    creator. ``PROTECT`` FKs from Dataset to Tag and from ProdTask to
    ProdConfig mean we cannot synthesize ad hoc — we pin to whatever's
    already locked in the DB. The prod team can replace these per-task
    when they bind a real Dataset/Config to a CSV-imported task.
    """
    def first_locked(model, label):
        t = model.objects.filter(status='locked').order_by('tag_number').first()
        if not t:
            raise ServiceError(f'No locked {label} tag available for CSV import')
        return t

    physics = first_locked(PhysicsTag, 'physics')
    evgen = first_locked(EvgenTag, 'evgen')
    simu = first_locked(SimuTag, 'simu')
    reco = first_locked(RecoTag, 'reco')

    cfg = (ProdConfig.objects.filter(name__icontains='26.02.0 Standard').first()
           or ProdConfig.objects.first())
    if not cfg:
        raise ServiceError('No ProdConfig available for CSV import anchor')

    campaign = Campaign.objects.filter(lifecycle='current').order_by('-updated_at').first()
    if not campaign:
        raise ServiceError('No current Campaign for CSV import')

    return physics, evgen, simu, reco, cfg, campaign


def import_default_datasets_csv(csv_path=None, *, created_by='csv_import'):
    """Import Sakib's epic-prod datasets.csv into the catalog.

    Each CSV row becomes (idempotently):
      - one Dataset (placeholder tags + ``26.02.0`` campaign labels,
        full row preserved in ``metadata['csv_import']``),
      - one ProdTask (status='csv_import', linked to the Dataset, the
        anchor ProdConfig, and the current Campaign).

    Idempotency key per row: ``(Dataset Path, Generator/Dataset Version)``.
    Re-running updates the existing rows in place.

    Returns dict::

        {'rows': int, 'created': int, 'updated': int, 'errors': [str, ...]}
    """
    path = csv_path or DEFAULT_DATASETS_CSV_PATH
    physics, evgen, simu, reco, cfg, campaign = _ensure_csvimport_anchors()

    with open(path, newline='') as f:
        rows = list(_csv.DictReader(f))

    summary = {'rows': len(rows), 'created': 0, 'updated': 0, 'errors': []}

    with transaction.atomic():
        for i, row in enumerate(rows, 1):
            ds_path = (row.get('Dataset Path') or '').strip()
            gen_ver = (row.get('Generator/Dataset Version') or '').strip()
            if not ds_path and not gen_ver:
                summary['errors'].append(f'row {i}: empty path and gen_version, skipped')
                continue

            slug = _csvimport_slug(ds_path, gen_ver)
            dataset_name = f'csv_import.{slug}'
            task_name = f'csv_import.{slug}'

            raw_priority = (row.get('Priority') or '').strip()
            try:
                priority = int(raw_priority) if raw_priority else None
            except (TypeError, ValueError):
                _log.warning('csv_import row %d: unparseable Priority %r', i, raw_priority)
                priority = None
            nevents = _parse_event_count(row.get('Number of Events'))

            metadata = {
                'stage': 'evgen',
                'source': {'kind': 'csv_manifest', 'location': ds_path},
                'csv_import': {k: (v or '').strip() for k, v in row.items()},
            }
            if gen_ver:
                metadata['source']['gen_version'] = gen_ver

            ds = Dataset.objects.filter(dataset_name=dataset_name, block_num=1).first()
            if ds:
                ds.description = (row.get('Description') or '').strip()
                ds.metadata = metadata
                ds.save()
                ds_created = False
            else:
                ds = Dataset(
                    dataset_name=dataset_name,
                    scope='group.EIC',
                    detector_version='26.02.0',
                    detector_config='epic_craterlake',
                    physics_tag=physics, evgen_tag=evgen,
                    simu_tag=simu, reco_tag=reco,
                    description=(row.get('Description') or '').strip(),
                    metadata=metadata,
                    created_by=created_by,
                )
                ds.save()
                ds_created = True

            task_defaults = dict(
                description=(row.get('Description') or '').strip(),
                status='csv_import',
                dataset=ds,
                prod_config=cfg,
                campaign=campaign,
                requestor=(row.get('DSC or PWG') or '').strip().upper(),
                priority=priority,
                pre_tdr_use=_possible(row.get('Pre-TDR Use')),
                early_science_use=_yesno(row.get('Early Science Use')),
                other_use=_possible(row.get('Other Use')),
                new_request=_yesno(row.get('New Request')),
                overrides={
                    'csv_import': {
                        'background': (row.get('Background') or '').strip(),
                        'nevents': nevents,
                        'issue_url': (row.get('Issue') or '').strip(),
                        'gen_version': gen_ver,
                        'evgen': _extract_evgen(gen_ver),
                        'other_use_text': (row.get('Other Use') or '').strip(),
                    },
                },
                created_by=created_by,
            )

            existing = ProdTask.objects.filter(name=task_name).first()
            if existing:
                # Preserve status once an operator has moved the task off
                # csv_import — re-imports must not roll back lifecycle.
                preserve_status = existing.status != 'csv_import'
                for k, v in task_defaults.items():
                    if k == 'status' and preserve_status:
                        continue
                    if k == 'overrides':
                        # Merge so non-csv_import override keys (added by
                        # operators or other code paths) are preserved;
                        # the csv_import bucket itself is fully refreshed.
                        merged = dict(existing.overrides or {})
                        merged.update(v)
                        v = merged
                    setattr(existing, k, v)
                existing.save()
                summary['updated'] += 1
            else:
                ProdTask.objects.create(name=task_name, **task_defaults)
                summary['created'] += 1

    return summary


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

    valid = _known_prodtask_statuses()
    if new_status not in valid:
        raise ServiceError(
            f'Invalid status. Choose from: {", ".join(sorted(valid))}'
        )
    task.status = new_status
    task.save(update_fields=['panda_task_id', 'status', 'updated_at'])
    return task
