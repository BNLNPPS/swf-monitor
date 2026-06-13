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
import os as _os
import re as _re

from django.conf import settings as _settings
from django.db import transaction

_log = _logging.getLogger(__name__)

from .models import (
    Dataset, ProdConfig, ProdTask,
    Campaign, ProdRequest,
    PhysicsCategory, PhysicsTag, EvgenTag, SimuTag, RecoTag, BackgroundTag,
)
from .physics_match import derive_physics, derive_background, derive_evgen, single_particle_angle


class ServiceError(Exception):
    """Domain error with an HTTP-shaped status hint and detail message."""
    def __init__(self, detail, status=400):
        self.detail = detail
        self.status = status
        super().__init__(detail)


# Allowed ProdTask lifecycle transitions. Submission and post-submission
# state changes are recorded via prodtask_record_submission and
# automation, not direct human transitions.
#
# Imported catalog rows enter as ``draft`` and follow the normal flow. The one
# import status that is NOT a key here is ``past_output``: a frozen historical
# archive (terminal — clone it, via Copy, to base new production on it), so the
# generic set-status path can never move it. See
# PCS_DATASET_REQUEST_WORKFLOW.md §Lifecycle and EPICPROD_TASK_CATALOG.md §6.
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
                   background_tag_label=None,
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
        tags[field] = tag

    if background_tag_label:
        bg = BackgroundTag.objects.filter(tag_label=background_tag_label).first()
        if not bg:
            raise ServiceError(f'background_tag not found: {background_tag_label}')
        tags['background_tag'] = bg

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


def prodtask_readiness_problems(task):
    """Reasons a task is NOT ready to lock/submit; empty list = ready.

    Checks what a valid PanDA submission needs, independent of how the task was
    built (composed tags or imported catalog metadata):

    - **Output.** The task must produce a physics output — at least one of
      ``copy_reco`` / ``copy_full``. A task that copies neither (logs only)
      produces nothing worth submitting.
    - **Physics is really bound.** An imported catalog row carries its real beam
      in its metadata (``overrides['csv_import']['filters']['beam']``, e.g.
      ``18x275``). The import pins every such row to one placeholder anchor
      physics tag, so if the bound tag's beam disagrees with the catalog beam,
      the physics is a placeholder and the submission artifact would carry the
      wrong beam. The physics automatch (``find_or_create_physics_tag`` wired
      into the importer) binds the right tag, so this clears on reload. Background
      rows are exempt: they stay parked on the anchor pending the ``k`` background
      tag type, so their beam mismatch is expected, not an error.
    """
    problems = []
    cfg = task.get_effective_config()
    if not (cfg.get('copy_reco') or cfg.get('copy_full')):
        problems.append('No physics output configured (enable copy of reco or full).')

    csv_filters = (((task.overrides or {}).get('csv_import') or {})
                   .get('filters') or {})
    catalog_beam = csv_filters.get('beam') or ''
    is_background = csv_filters.get('physics') == 'BACKGROUNDS'
    if catalog_beam and not is_background and task.dataset_id and task.dataset.physics_tag_id:
        pt = task.dataset.physics_tag
        p = pt.parameters or {}
        e = str(p.get('beam_energy_electron', '')).strip()
        h = str(p.get('beam_energy_hadron', '')).strip()
        tag_beam = f'{e}x{h}' if e and h else ''
        if tag_beam and tag_beam != catalog_beam:
            problems.append(
                f'Wrong physics tag: this sample is {catalog_beam} but it is '
                f'tagged {pt.tag_label} ({tag_beam}). Assign the matching '
                f'physics tag.')
    return problems


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
    # Readiness gate: locking (→ ready) requires a submittable task. One
    # chokepoint for the lock action, the detail-page lock, and REST/MCP
    # set-status. See prodtask_readiness_problems.
    if new_status == 'ready' and task.status != 'ready':
        problems = prodtask_readiness_problems(task)
        if problems:
            raise ServiceError('Cannot lock — task is not ready: ' + ' '.join(problems))

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


# Common prefix on every catalog source path; carries no distinguishing
# information, so it is stripped from the human-readable task name (and the
# compose title strips it the same way).
EPIC_VOLATILE_PREFIX = '/volatile/eic/EPIC/'


def _task_name_from_path(path):
    """Human-readable ProdTask.name from a catalog source path: the path with
    the common ``/volatile/eic/EPIC/`` prefix stripped (matching the compose
    title). Paths are unique per catalog row. Returns '' for an empty path so
    the caller can fall back to the slug name."""
    p = (path or '').strip()
    if p.startswith(EPIC_VOLATILE_PREFIX):
        p = p[len(EPIC_VOLATILE_PREFIX):]
    return p


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
        t = model.objects.order_by('tag_number').first()
        if not t:
            raise ServiceError(f'No {label} tag available for CSV import')
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


# Process -> physics category digit, for numbering NEWLY created tags
# (number = digit*1000 + global suffix). Existing tags are reused by their
# physics parameters and keep their historically-assigned numbers/categories;
# the seeded set predates this map and is internally inconsistent — renumbering
# it is a separate cleanup, deliberately out of scope here.
_PROCESS_CATEGORY = {
    'SINGLE': 1,
    'DIS': 2, 'DIS_NC': 2, 'DIS_CC': 2, 'DDIS': 2,
    'DVCS': 3, 'DDVCS': 3, 'TCS': 3,
    'SIDIS': 4, 'SIDIS_D0': 4, 'SIDIS_DIJET': 4, 'SIDIS_Lc': 4,
    'DEMP': 5, 'DVMP': 5, 'MESON_SF': 5,
    'DIFFRACTIVE_JPSI': 5, 'DIFFRACTIVE_PHI': 5, 'DIFFRACTIVE_RHO': 5,
    'PHOTOPRODUCTION_JPSI': 5, 'UPSILON': 5,
    'UCHANNEL_PI0': 5, 'UCHANNEL_RHO': 5, 'ALP': 5,
}


#: Every physics axis that defines a tag's identity. Matching compares the full
#: set (a missing axis counts as ''), so a tag is reused only when every physics
#: axis agrees — the old coarse (process, beam) key is what collapsed Q2 range,
#: decay/charge, beam config, and the rest onto the first-created tag.
_PHYSICS_MATCH_FIELDS = (
    'process', 'beam_energy_electron', 'beam_energy_hadron',
    'beam_species', 'nucleon', 'q2_range', 'decay_mode', 'hadron_charge',
    'helicity', 'polarization', 'coherence', 'model', 'beam_config',
    'state', 'mechanism', 'final_state', 'channel', 'mass',
    'particle', 'gun_energy',
)


def _physics_key(params):
    """Identity tuple over the full physics axis set (a missing axis == '')."""
    return tuple((params or {}).get(f, '') for f in _PHYSICS_MATCH_FIELDS)


def find_or_create_physics_tag(derived, *, created_by='csv_import', dry_run=False):
    """Resolve the physics tag whose full parameter set equals ``derived``.

    Matching keys on the complete physics axis set, not the ``(process, beam)``
    subset: two compositions differing in any axis (Q2 range, species, decay,
    beam config, ...) are different tags. A coarse DB filter on ``process``
    narrows candidates; the full set is compared in Python so a tag stored with
    only the axes it carries still matches (a missing axis counts as ''). Among
    matches a locked tag is preferred, then the lowest number. A miss creates a
    new *draft* tag, numbered via ``PhysicsTag.allocate_next`` and the category
    map; locking is a deliberate later step at submission prep, not here.

    Returns ``(tag, action)`` with action ``'reuse'`` | ``'create'``. In
    ``dry_run`` nothing is written and a ``'create'`` returns ``(None, 'create')``.
    """
    process = derived.get('process')
    want = _physics_key(derived)
    matches = sorted(
        (t for t in PhysicsTag.objects.filter(parameters__process=process)
         if _physics_key(t.parameters) == want),
        key=lambda t: (0 if t.status == 'locked' else 1, t.tag_number),
    )
    if matches:
        return matches[0], 'reuse'

    digit = _PROCESS_CATEGORY.get(process)
    if digit is None:
        raise ServiceError(f'No physics category mapping for process {process!r}')
    if dry_run:
        return None, 'create'
    category = PhysicsCategory.objects.get(digit=digit)
    tag = PhysicsTag(
        tag_number=PhysicsTag.allocate_next(category),
        category=category,
        status='draft',
        parameters=derived,
        created_by=created_by,
    )
    tag.save()
    return tag, 'create'


def _no_signal_physics_tag():
    """The locked, signal-free physics tag (``p6001``) that standalone background
    datasets name in the physics slot. Seeded by migration 0010."""
    tag = PhysicsTag.objects.filter(tag_label='p6001').first()
    if not tag:
        raise ServiceError('No-signal physics tag p6001 is missing; run migrations.')
    return tag


#: Background-tag dedup key — the sample-defining params.
_BG_MATCH_FIELDS = ('background_type', 'bg_source', 'bg_mechanism', 'bg_generator',
                    'beam_energy_electron', 'beam_energy_hadron')


def _background_description(p):
    """Comma-joined description synthesised from background-tag parameters:
    generator, type, beam energy, source, mechanism — each included only when
    present. Single-beam samples show just the present energy."""
    e = p.get('beam_energy_electron', 'N/A')
    h = p.get('beam_energy_hadron', 'N/A')
    if e != 'N/A' and h != 'N/A':
        beam = f'{e}x{h} GeV'
    elif e != 'N/A':
        beam = f'{e} GeV'
    elif h != 'N/A':
        beam = f'{h} GeV'
    else:
        beam = ''
    parts = [
        p.get('bg_generator', ''),
        p.get('background_type', ''),
        beam,
        p.get('bg_source', ''),
        p.get('bg_mechanism', ''),
    ]
    return ', '.join(x for x in parts if x)


def find_or_create_background_tag(params, *, created_by='csv_import', dry_run=False):
    """Resolve the background (k) tag for derived background params.

    Mirrors ``find_or_create_physics_tag``: match by the sample-defining
    parameters, prefer a locked tag then the lowest number, or create a new
    *draft* tag. Returns ``(tag, action)`` with action ``'reuse'`` | ``'create'``.
    """
    match = {f'parameters__{k}': params.get(k, '') for k in _BG_MATCH_FIELDS}
    matches = sorted(
        BackgroundTag.objects.filter(**match),
        key=lambda t: (0 if t.status == 'locked' else 1, t.tag_number),
    )
    if matches:
        tag = matches[0]
        # Backfill a synthesised description if missing (annotation, not a
        # reproducibility field) — never overwrite an existing one.
        if not tag.description and not dry_run:
            tag.description = _background_description(params)
            tag.save(update_fields=['description', 'updated_at'])
        return tag, 'reuse'
    if dry_run:
        return None, 'create'
    tag = BackgroundTag(
        tag_number=BackgroundTag.allocate_next(),
        status='draft',
        description=_background_description(params),
        parameters=params,
        created_by=created_by,
    )
    tag.save()
    return tag, 'create'


def find_or_create_evgen_tag(params, *, created_by='csv_import', dry_run=False):
    """Resolve the evgen (e) tag for derived ``{generator, generator_version}``
    params. Mirrors ``find_or_create_physics_tag``: match by (generator,
    generator_version), prefer locked then lowest number, or create a new *draft*
    tag. Returns ``(tag, action)`` with action ``'reuse'`` | ``'create'``."""
    match = {
        'parameters__generator': params.get('generator', ''),
        'parameters__generator_version': params.get('generator_version', ''),
    }
    matches = sorted(
        EvgenTag.objects.filter(**match),
        key=lambda t: (0 if t.status == 'locked' else 1, t.tag_number),
    )
    if matches:
        return matches[0], 'reuse'
    if dry_run:
        return None, 'create'
    tag = EvgenTag(
        tag_number=EvgenTag.allocate_next(),
        status='draft',
        description=f"{params.get('generator', '')} {params.get('generator_version', '')}".strip(),
        parameters=params,
        created_by=created_by,
    )
    tag.save()
    return tag, 'create'


def import_default_datasets_csv(csv_path=None, *, created_by='csv_import'):
    """Import Sakib's epic-prod datasets.csv into the catalog.

    Each CSV row becomes (idempotently):
      - one Dataset (placeholder tags + ``26.02.0`` campaign labels,
        full row preserved in ``metadata['csv_import']``),
      - one ProdTask (status='draft', linked to the Dataset, the
        anchor ProdConfig, and the current Campaign) — imported rows enter
        the editable production lifecycle directly.

    Idempotency key per row: ``(Dataset Path, Generator/Dataset Version)``.
    Re-running updates the existing rows in place.

    Returns dict::

        {'rows': int, 'created': int, 'updated': int, 'errors': [str, ...]}
    """
    path = csv_path or DEFAULT_DATASETS_CSV_PATH
    physics, evgen, simu, reco, cfg, campaign = _ensure_csvimport_anchors()

    with open(path, newline='') as f:
        rows = list(_csv.DictReader(f))

    summary = {'rows': len(rows), 'created': 0, 'updated': 0,
               'tag_actions': {}, 'errors': []}

    with transaction.atomic():
        for i, row in enumerate(rows, 1):
            ds_path = (row.get('Dataset Path') or '').strip()
            gen_ver = (row.get('Generator/Dataset Version') or '').strip()
            if not ds_path and not gen_ver:
                summary['errors'].append(f'row {i}: empty path and gen_version, skipped')
                continue

            slug = _csvimport_slug(ds_path, gen_ver)
            dataset_name = f'csv_import.{slug}'
            # Human-readable task name = the source path (the row's title), not
            # an opaque hash, so URLs read clearly and the name can't be mistaken
            # for the importer firing. Falls back to the slug name for a path-less
            # (gen_version-only) row. The Rucio DID stays slug-based on
            # dataset_name — its charset rules are a separate concern.
            task_name = _task_name_from_path(ds_path) or dataset_name

            # Derive the real physics from the path and resolve its locked tag,
            # replacing the placeholder anchor. Backgrounds resolve to the
            # signal-free p6001 physics tag plus a derived k background tag; an
            # unrecognizable path keeps the anchor and is surfaced in errors
            # rather than silently mis-tagged.
            filters = _extract_csv_filters(ds_path, 'epic_craterlake')
            derived = derive_physics(task_name, beam=filters.get('beam', ''))
            is_background = bool(derived) and derived.get('process') in ('BEAMGAS', 'SYNRAD')
            angular_range = single_particle_angle(task_name)
            row_physics_tag = physics
            row_background_tag = None
            if derived is None:
                summary['errors'].append(
                    f'row {i}: path {task_name!r} is not a recognizable EVGEN '
                    f'entry; left on placeholder tag {physics.tag_label}')
            elif is_background:
                # Standalone background: physics slot is the signal-free p6001
                # sentinel; the background config itself becomes a k tag.
                row_physics_tag = _no_signal_physics_tag()
                bg_params = derive_background(task_name)
                if bg_params:
                    row_background_tag, bg_action = find_or_create_background_tag(
                        bg_params, created_by=created_by)
                    bg_key = f'bg-{bg_action}'
                    summary['tag_actions'][bg_key] = summary['tag_actions'].get(bg_key, 0) + 1
            else:
                row_physics_tag, action = find_or_create_physics_tag(
                    derived, created_by=created_by)
                summary['tag_actions'][action] = summary['tag_actions'].get(action, 0) + 1

            # Resolve the generator (evgen) from path/gen_version. Unresolved
            # (ambiguous/underspecified) rows keep the placeholder anchor and are
            # left for manual association — never guessed. See the curated
            # derive_evgen and docs (campaign->tag mapping).
            row_evgen_tag = evgen
            ev_params = derive_evgen(task_name, gen_ver)
            if ev_params:
                row_evgen_tag, ev_action = find_or_create_evgen_tag(
                    ev_params, created_by=created_by)
                ev_key = f'evgen-{ev_action}'
                summary['tag_actions'][ev_key] = summary['tag_actions'].get(ev_key, 0) + 1

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
                ds.physics_tag = row_physics_tag
                ds.evgen_tag = row_evgen_tag
                ds.background_tag = row_background_tag
                ds.save()
                ds_created = False
            else:
                ds = Dataset(
                    dataset_name=dataset_name,
                    scope='group.EIC',
                    detector_version='26.02.0',
                    detector_config='epic_craterlake',
                    physics_tag=row_physics_tag, evgen_tag=row_evgen_tag,
                    simu_tag=simu, reco_tag=reco,
                    background_tag=row_background_tag,
                    description=(row.get('Description') or '').strip(),
                    metadata=metadata,
                    created_by=created_by,
                )
                ds.save()
                ds_created = True

            task_defaults = dict(
                description=(row.get('Description') or '').strip(),
                # Imported catalog rows enter the editable production lifecycle
                # directly — turning Sakib's static default_datasets.csv list
                # into the living, actionable current-campaign catalog is the
                # whole point of the import. (The legacy 'csv_import' holding
                # status + per-row Adopt step were retired; see migration 0008.)
                status='draft',
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
                        'filters': filters,
                        'angular_range': angular_range,
                    },
                },
                created_by=created_by,
            )

            existing = ProdTask.objects.filter(name=task_name).first()
            if existing:
                # A re-import refreshes the catalog data but never moves the
                # task in its lifecycle — whatever state an operator has
                # reached (draft/ready/submitted/…) is left untouched.
                for k, v in task_defaults.items():
                    if k == 'status':
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


# ---------------------------------------------------------------------------
# Past-campaign output ingest (epic-prod FULL/RECO/<version>/index.md)
# ---------------------------------------------------------------------------

EPIC_PROD_PATH = '/data/wenauseic/github/epic-prod'
PAST_CAMPAIGN_STAGES = ('FULL', 'RECO')
PAST_CAMPAIGN_YEAR_PREFIXES = ('25.', '26.')   # 2025 + 2026 campaigns

_PAST_DID_RE   = _re.compile(r'^===\s*(epic:/\S+)\s*===\s*$')
_PAST_RSE_RE   = _re.compile(r'RSE:\s*(\S+)\s+Files:\s*(\d+)/(\d+)\s*\(([^)]+)\)')
_PAST_SIZE_RE  = _re.compile(r'Total Size:\s*([\d.]+)\s*([KMGTP]?B)\s*\((\d+)\s*files?\)', _re.I)
_PAST_SUMMARY_RE = _re.compile(r'^===\s*CAMPAIGN SUMMARY\s*===\s*$')

_SIZE_UNIT_BYTES = {'B': 1, 'KB': 1_000, 'MB': 1_000_000,
                    'GB': 1_000_000_000, 'TB': 1_000_000_000_000,
                    'PB': 1_000_000_000_000_000}


def _parse_size_to_bytes(value, unit):
    try:
        n = float(value)
    except (TypeError, ValueError):
        _log.warning('past_ingest: unparseable size value %r', value)
        return 0
    mult = _SIZE_UNIT_BYTES.get(unit.upper())
    if mult is None:
        _log.warning('past_ingest: unrecognised size unit %r', unit)
        return 0
    return int(round(n * mult))


def _parse_past_index(text):
    """Parse an epic-prod docs/<STAGE>/<version>/index.md file.

    Yields dicts: {did, rses: [{name, files, total, status}], file_count,
    data_size_bytes, complete}. The campaign summary block at the end is
    skipped.
    """
    block = None

    def _emit():
        nonlocal block
        if block:
            yield_block, block = block, None
            return yield_block
        return None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line == '```':
            continue
        if _PAST_SUMMARY_RE.match(line):
            done = _emit()
            if done is not None:
                yield done
            break
        m = _PAST_DID_RE.match(line)
        if m:
            done = _emit()
            if done is not None:
                yield done
            block = {'did': m.group(1), 'rses': [], 'file_count': 0,
                     'data_size_bytes': 0, 'complete': True}
            continue
        if block is None:
            continue
        m = _PAST_RSE_RE.search(line)
        if m:
            files, total, status = int(m.group(2)), int(m.group(3)), m.group(4).strip()
            block['rses'].append({'name': m.group(1), 'files': files,
                                  'total': total, 'status': status})
            if status != 'complete':
                block['complete'] = False
            continue
        m = _PAST_SIZE_RE.search(line)
        if m:
            block['data_size_bytes'] = _parse_size_to_bytes(m.group(1), m.group(2))
            block['file_count'] = int(m.group(3))
            continue
    done = _emit()
    if done is not None:
        yield done


_PAST_BEAM_RE = _re.compile(r'(\d+x\d+)')
_PAST_Q2_RE   = _re.compile(r'(minQ2=\d+|q2_\d+(?:to\d+)?)')
_PAST_PHYS_TOP = ('DIS', 'SIDIS', 'DDIS', 'EXCLUSIVE', 'SINGLE', 'BACKGROUNDS')


_PAST_ENERGY_BARE_RE = _re.compile(
    r'\b(\d+(?:\.\d+)?(?:eV|keV|MeV|GeV|TeV))\b', _re.I)


def _extract_path_filters(segments):
    """Extract {beam, physics, q2, species, energy} from a list of path
    segments. Shared by the past-output DID and the current-tab CSV
    input-dataset path so both filter bars speak the same vocabulary.
    """
    seg_str = '/'.join(segments)
    beam_m = _PAST_BEAM_RE.search(seg_str)
    q2_m   = _PAST_Q2_RE.search(seg_str)
    physics = next((p for p in _PAST_PHYS_TOP if p in segments), '')
    species, energy = '', ''
    if physics == 'SINGLE':
        i = segments.index('SINGLE')
        if len(segments) > i + 1:
            species = segments[i + 1]
        if len(segments) > i + 2:
            energy = segments[i + 2]
    if not energy:
        em = _PAST_ENERGY_BARE_RE.search(seg_str)
        if em:
            energy = em.group(1)
    # Fold DIS subtype (NC / CC) directly into physics so the Physics
    # filter shows NC and CC as siblings of DIS, not a separate row.
    dis_type = next((t for t in ('NC', 'CC') if t in segments), '')
    if physics == 'DIS' and dis_type:
        physics = dis_type
    return {
        'beam':    beam_m.group(1) if beam_m else '',
        'physics': physics,
        'q2':      q2_m.group(1) if q2_m else '',
        'species': species,
        'energy':  energy,
    }


def _extract_past_filters(did):
    """Past-output filter fields: detector (from path segment 3) plus
    the shared {beam, physics, q2, species, energy}. Empty string for
    any dimension the path doesn't carry."""
    parts = did.split(':', 1)
    rest_str = (parts[1] if len(parts) == 2 else did).lstrip('/')
    segs = rest_str.split('/')
    detector = segs[2] if len(segs) > 2 else ''
    out = _extract_path_filters(segs[3:])
    out['detector'] = detector
    return out


def _extract_csv_filters(path, detector_config):
    """Current-tab filter fields from a CSV input dataset path.

    Path shape: /volatile/eic/EPIC/EVGEN/<physics>/<gen>/.../<beam>/...
    Geometry comes from the dataset's detector_config field (not the
    path); everything else from the path tail after the EVGEN prefix.
    """
    segs = (path or '').lstrip('/').split('/')
    # Drop the fixed prefix volatile/eic/EPIC/EVGEN when present, but
    # be tolerant if a different shape appears later.
    if len(segs) >= 4 and segs[:4] == ['volatile', 'eic', 'EPIC', 'EVGEN']:
        tail = segs[4:]
    else:
        tail = segs
    out = _extract_path_filters(tail)
    out['detector'] = detector_config or ''
    return out


def _decompose_past_did(did):
    """Break an epic-prod DID into the path-level fields we filter on.

    Expected form: epic:/STAGE/version/detector_config/<bg-chain>/<phys-chain>/beam/paramset
    Everything after detector_config is best-effort; what we don't recognise
    stays in metadata['past_output']['path_remainder'] for the row to render
    verbatim. NO-SILENT-FAILURES: a DID that doesn't even split into the
    leading STAGE/version/detector parts is logged.
    """
    parts = did.split(':', 1)
    rest = parts[1] if len(parts) == 2 else did
    rest = rest.lstrip('/')
    segs = rest.split('/')
    if len(segs) < 3:
        _log.warning('past_ingest: DID %r has too few segments', did)
        return {}
    out = {'stage': segs[0], 'version': segs[1], 'detector_config': segs[2]}
    if len(segs) > 3:
        out['path_remainder'] = '/'.join(segs[3:])
    return out


def _version_tuple(v):
    """'26.06.0' -> (26, 6, 0) for release comparison; non-numeric parts sort low."""
    return tuple(int(p) if p.isdigit() else -1 for p in str(v or '').split('.'))


def import_epic_prod_past_campaigns(*, epic_prod_path=EPIC_PROD_PATH,
                                    created_by='past_import'):
    """Import 2026 past-campaign output datasets from a cloned epic-prod.

    Walks ``docs/FULL/<v>/index.md`` and ``docs/RECO/<v>/index.md`` for
    every 2026 version listed in ``docs/_data/{full,reco}_content.yml``.
    The 'main' alias is excluded (it's a moving target, not a frozen
    archive).

    For each parsed dataset we get-or-create:
      - Campaign(name='{STAGE}/{version}', lifecycle='past')
      - Dataset(did=PCS internal DID, dataset_name='past.{STAGE}.{ver}.{slug}'),
        with the epic-prod Rucio DID stored in metadata['source']['location']
        and the per-RSE breakdown in metadata['past_output'].
      - ProdTask(name='past.{STAGE}.{ver}.{slug}', status='past_output',
        linked to the Dataset, anchor ProdConfig, and Campaign).

    Idempotency key: (STAGE, version, epic_prod_did). Re-running refreshes
    file_count / data_size / rse breakdown but leaves any operator-touched
    status / overrides intact (same rule as csv_import).
    """
    import os as _os
    import yaml as _yaml
    physics, evgen, simu, reco, cfg, _ = _ensure_csvimport_anchors()
    current_camp = Campaign.objects.filter(lifecycle='current').first()
    current_v = _version_tuple(current_camp.name) if current_camp else None

    summary = {'campaigns': 0, 'rows': 0, 'created': 0, 'updated': 0, 'errors': []}

    versions_by_stage = {}
    for stage in PAST_CAMPAIGN_STAGES:
        yml_name = f'{stage.lower()}_content.yml'
        yml_path = _os.path.join(epic_prod_path, 'docs', '_data', yml_name)
        try:
            with open(yml_path) as f:
                entries = _yaml.safe_load(f) or []
        except (OSError, _yaml.YAMLError) as e:
            summary['errors'].append(f'{stage}: cannot read {yml_name}: {e}')
            continue
        versions_by_stage[stage] = [
            e['text'] for e in entries
            if isinstance(e, dict)
            and any(e.get('text', '').startswith(p) for p in PAST_CAMPAIGN_YEAR_PREFIXES)
            and e['text'] != 'main'
        ]

    with transaction.atomic():
        for stage, versions in versions_by_stage.items():
            for version in versions:
                campaign_name = f'{stage}/{version}'
                index_path = _os.path.join(epic_prod_path, 'docs', stage, version, 'index.md')
                try:
                    with open(index_path) as f:
                        text = f.read()
                except OSError as e:
                    summary['errors'].append(f'{campaign_name}: {e}')
                    continue

                # A produced release newer than the current campaign is a
                # FUTURE release, not past — classify by version so the lifecycle
                # self-corrects on every import and as current advances.
                lc = 'future' if (current_v is not None
                                  and _version_tuple(version) > current_v) else 'past'
                campaign, _ = Campaign.objects.get_or_create(
                    name=campaign_name,
                    defaults={'lifecycle': lc,
                              'description': f'{stage} campaign {version} '
                                             f'(epic-prod {index_path})',
                              'created_by': created_by},
                )
                if campaign.lifecycle != lc:
                    campaign.lifecycle = lc
                    campaign.save(update_fields=['lifecycle'])
                summary['campaigns'] += 1

                campaign_files = 0
                campaign_bytes = 0
                for block in _parse_past_index(text):
                    summary['rows'] += 1
                    epic_did = block['did']
                    slug = _hashlib.sha1(epic_did.encode()).hexdigest()[:12]
                    pcs_name = f'past.{stage}.{version}.{slug}'
                    decomposed = _decompose_past_did(epic_did)

                    metadata = {
                        'stage': stage.lower(),
                        'source': {'kind': 'rucio_did', 'location': epic_did},
                        'past_output': {
                            'campaign_name': campaign_name,
                            'stage': stage,
                            'version': version,
                            'rses': block['rses'],
                            'complete': block['complete'],
                            'path': decomposed,
                            'filters': _extract_past_filters(epic_did),
                            'index_path': index_path,
                        },
                    }

                    # Derive the real physics from the DID path remainder and bind
                    # the matching tag, replacing the placeholder anchor (the p1006
                    # dump). Standalone backgrounds take the signal-free p6001 tag;
                    # an unparseable remainder keeps the anchor and is surfaced.
                    # Evgen and background k tags for past rows stay a separate
                    # (manual) association.
                    remainder = decomposed.get('path_remainder', '')
                    derived = derive_physics(
                        remainder, beam=metadata['past_output']['filters'].get('beam', ''))
                    row_physics_tag = physics
                    if derived is None:
                        summary['errors'].append(
                            f'{campaign_name}: DID {epic_did!r} physics unresolved; '
                            f'left on placeholder {physics.tag_label}')
                    elif derived.get('process') in ('BEAMGAS', 'SYNRAD'):
                        row_physics_tag = _no_signal_physics_tag()
                    else:
                        row_physics_tag, _ = find_or_create_physics_tag(
                            derived, created_by=created_by)

                    pcs_did = f'group.EIC:{pcs_name}.b1'
                    ds, ds_created = Dataset.objects.get_or_create(
                        dataset_name=pcs_name, block_num=1,
                        defaults=dict(
                            scope='group.EIC', did=pcs_did,
                            detector_version=version,
                            detector_config=decomposed.get('detector_config', ''),
                            physics_tag=row_physics_tag, evgen_tag=evgen,
                            simu_tag=simu, reco_tag=reco,
                            file_count=block['file_count'],
                            data_size=block['data_size_bytes'],
                            description='',
                            metadata=metadata,
                            created_by=created_by,
                        ),
                    )
                    if not ds_created:
                        ds.file_count = block['file_count']
                        ds.data_size = block['data_size_bytes']
                        ds.metadata = metadata
                        ds.detector_version = version
                        ds.detector_config = decomposed.get('detector_config', '')
                        ds.physics_tag = row_physics_tag
                        ds.save()

                    task_defaults = dict(
                        description='',
                        dataset=ds,
                        prod_config=cfg,
                        campaign=campaign,
                        overrides={'past_output': metadata['past_output']},
                        created_by=created_by,
                    )
                    existing = ProdTask.objects.filter(name=pcs_name).first()
                    if existing:
                        preserve_status = existing.status != 'past_output'
                        for k, v in task_defaults.items():
                            if k == 'overrides':
                                merged = dict(existing.overrides or {})
                                merged.update(v)
                                v = merged
                            setattr(existing, k, v)
                        if not preserve_status:
                            existing.status = 'past_output'
                        existing.save()
                        summary['updated'] += 1
                    else:
                        ProdTask.objects.create(name=pcs_name,
                                                status='past_output',
                                                **task_defaults)
                        summary['created'] += 1
                    campaign_files += block['file_count']
                    campaign_bytes += block['data_size_bytes']

                campaign.data = {
                    **(campaign.data or {}),
                    'past_summary': {
                        'file_count': campaign_files,
                        'data_size_bytes': campaign_bytes,
                        'stage': stage,
                        'version': version,
                    },
                }
                campaign.save(update_fields=['data', 'updated_at'])

    return summary


# ---------------------------------------------------------------------------
# JLab Rucio current-campaign snapshot
#
# Output datasets land at JLab Rucio under scope `epic`. The nightly
# epic-prod GitHub Action that generates docs/{FULL,RECO}/<v>/index.md is
# exactly this same Rucio query, dumped to markdown. We pull it directly
# so the Current tab can show 'Output: <files / size / RSEs>' on each
# task row without waiting for the upstream nightly rebuild.
#
# Credentials are the public read-only eicread/eicread account
# (matches the PandaBot jlab-rucio MCP config). Override via env.
# ---------------------------------------------------------------------------

JLAB_RUCIO_URL      = '/'.join(['https://rucio-server.jlab.org:443'])
JLAB_RUCIO_ACCOUNT  = 'eicread'
JLAB_RUCIO_USERNAME = 'eicread'
JLAB_RUCIO_PASSWORD = 'eicread'
RUCIO_SNAPSHOT_DIR  = _os.path.join(_settings.SWF_TMP_DIR, 'rucio-snapshots')


def _jlab_rucio_auth(timeout=30):
    """userpass-auth against JLab Rucio. Returns the X-Rucio-Auth-Token."""
    import urllib.request as _ur
    import ssl as _ssl
    import os as _os
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    url = (_os.environ.get('JLAB_RUCIO_URL') or JLAB_RUCIO_URL) + '/auth/userpass'
    req = _ur.Request(url)
    req.add_header('X-Rucio-Account',  _os.environ.get('JLAB_RUCIO_ACCOUNT',  JLAB_RUCIO_ACCOUNT))
    req.add_header('X-Rucio-Username', _os.environ.get('JLAB_RUCIO_USERNAME', JLAB_RUCIO_USERNAME))
    req.add_header('X-Rucio-Password', _os.environ.get('JLAB_RUCIO_PASSWORD', JLAB_RUCIO_PASSWORD))
    resp = _ur.urlopen(req, context=ctx, timeout=timeout)
    token = resp.headers['X-Rucio-Auth-Token']
    if not token:
        raise ServiceError('JLab Rucio auth returned no token')
    return token


def _jlab_rucio_get(path, token, *, timeout=60, **q):
    """GET a JLab Rucio path with the auth token; returns response text."""
    import urllib.request as _ur
    import urllib.parse as _up
    import ssl as _ssl
    import os as _os
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    url = (_os.environ.get('JLAB_RUCIO_URL') or JLAB_RUCIO_URL) + path
    if q:
        url += '?' + _up.urlencode(q)
    req = _ur.Request(url)
    req.add_header('X-Rucio-Auth-Token', token)
    return _ur.urlopen(req, context=ctx, timeout=timeout).read().decode()


def _ndjson(text):
    """Parse a newline-delimited-JSON Rucio response (strings or dicts)."""
    import json as _json
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(_json.loads(line))
        except _json.JSONDecodeError:
            out.append(line)
    return out


def fetch_jlab_rucio_campaign(campaign_path, *, scope='epic', token=None,
                              max_workers=16):
    """Fetch the full Rucio snapshot for one campaign path (e.g. '/RECO/26.02.0').

    Returns {count, datasets:[{did, length, bytes, rse_replicas:[{rse, ...}]}, ...]}.
    Each dataset's rse_replicas mirrors what /replicas/<scope>/<name>/datasets
    returns — per-RSE found/total/state/bytes, exactly the shape the
    epic-prod nightly workflow uses to produce its index.md replica lines.

    Per-dataset metadata + replica fetches run in a ThreadPoolExecutor so a
    365-dataset campaign completes in ~5-10s instead of ~80s, keeping the
    'Update from Rucio' button under Apache's request timeout.
    """
    import json as _json
    from concurrent.futures import ThreadPoolExecutor as _Pool
    if token is None:
        token = _jlab_rucio_auth()
    names = _ndjson(_jlab_rucio_get(
        f'/dids/{scope}/dids/search', token,
        type='dataset', name=campaign_path + '/*'))

    def _one(name):
        if not isinstance(name, str):
            return None
        try:
            meta = _json.loads(_jlab_rucio_get(f'/dids/{scope}/{name}', token))
        except Exception as e:                                # noqa: BLE001
            _log.warning('rucio meta %s/%s: %s', scope, name, e)
            meta = {}
        try:
            rse_records = _ndjson(
                _jlab_rucio_get(f'/replicas/{scope}/{name}/datasets', token))
        except Exception as e:                                # noqa: BLE001
            _log.warning('rucio replicas %s/%s: %s', scope, name, e)
            rse_records = []
        return {
            'did':          f'{scope}:{name}',
            'length':       meta.get('length'),
            'bytes':        meta.get('bytes'),
            'rse_replicas': rse_records,
        }

    with _Pool(max_workers=max_workers) as pool:
        results = list(pool.map(_one, names))
    datasets = [r for r in results if r is not None]
    return {'count': len(datasets), 'datasets': datasets}


def fetch_jlab_rucio_did(scope, name):
    """Live read of a single JLab Rucio DID for the self-hosted detail page.

    Pure read over the public eicread userpass — no agent credential, no cache;
    what it returns is true *now*. Generic over DID type: an input EVGEN dataset
    and an output RECO dataset render through this same path. Returns Rucio's
    full ``/meta`` (system + user attributes), the per-RSE dataset replicas, and
    a derived summary (type, total bytes, file count). The file list is fetched
    separately (``fetch_jlab_rucio_did_files``) — a populated RECO dataset has
    thousands of files. Raises ServiceError(404) for an unknown DID, (502) if
    JLab Rucio is unreachable. See docs/EPICPROD_DATA_LINEAGE.md."""
    import json as _json
    import urllib.error as _ue
    name = '/' + name.lstrip('/')   # Rucio names are leading-slashed; a proxy may collapse '//'
    try:
        token = _jlab_rucio_auth()
        meta = _json.loads(_jlab_rucio_get(f'/dids/{scope}/{name}/meta', token))
        replicas = [r for r in _ndjson(
            _jlab_rucio_get(f'/replicas/{scope}/{name}/datasets', token))
            if isinstance(r, dict)]
    except _ue.HTTPError as e:
        if e.code == 404:
            raise ServiceError(
                f'DID not found in JLab Rucio: {scope}:{name}', status=404)
        raise ServiceError(
            f'JLab Rucio error {e.code} for {scope}:{name}', status=502)
    except (_ue.URLError, OSError) as e:
        raise ServiceError(f'Could not reach JLab Rucio: {e}', status=502)
    # The DID record's length/bytes are often null; the replica rows carry the
    # real totals. Take the max across RSEs as the dataset total.
    def _from_replicas(key):
        vals = [r.get(key) for r in replicas if isinstance(r.get(key), int)]
        return max(vals) if vals else meta.get(key)
    return {
        'scope': scope, 'name': name, 'did': f'{scope}:{name}',
        'type': meta.get('did_type') or meta.get('type'),
        'account': meta.get('account'),
        'is_open': meta.get('is_open'),
        'availability': meta.get('availability'),
        'bytes': _from_replicas('bytes'),
        'file_count': _from_replicas('length'),
        'created_at': meta.get('created_at'),
        'updated_at': meta.get('updated_at'),
        'meta': meta,
        'replicas': replicas,
    }


def fetch_jlab_rucio_did_files(scope, name):
    """Live file list for a JLab Rucio DID (on-demand; can be thousands).

    Pure read; returns ``[{name, bytes, adler32, guid, events}, ...]``. Per-file
    PFN resolution is intentionally omitted here — the bulk ``/replicas`` call is
    too slow for a large dataset (it times out); the RSE-level replicas from
    ``fetch_jlab_rucio_did`` already answer 'where does it live'."""
    import urllib.error as _ue
    name = '/' + name.lstrip('/')
    try:
        token = _jlab_rucio_auth()
        files = [f for f in _ndjson(
            _jlab_rucio_get(f'/dids/{scope}/{name}/files', token, timeout=120))
            if isinstance(f, dict)]
    except _ue.HTTPError as e:
        if e.code == 404:
            raise ServiceError(
                f'DID not found in JLab Rucio: {scope}:{name}', status=404)
        raise ServiceError(
            f'JLab Rucio error {e.code} for {scope}:{name}', status=502)
    except (_ue.URLError, OSError) as e:
        raise ServiceError(f'Could not reach JLab Rucio: {e}', status=502)
    return files


def _request_input_tail(ds_path):
    """Return the comparable tail of a CSV input dataset path.

    /volatile/eic/EPIC/EVGEN/<TAIL>  ->  '<TAIL>'  (lower-cased)
    anything else                    ->  '' (no match)
    """
    if not ds_path:
        return ''
    parts = ds_path.strip('/').split('/')
    if len(parts) >= 5 and parts[:4] == ['volatile', 'eic', 'EPIC', 'EVGEN']:
        return '/'.join(parts[4:]).lower()
    return ''


def _did_path_tail(did):
    """Drop scope + /STAGE/<v>/<detector>/ and return the rest, lower-cased.

    epic:/RECO/26.02.0/epic_craterlake/DDIS/rapgap3.../noRad/ep/10x100/q2_1to10
        -> 'ddis/rapgap3.../norad/ep/10x100/q2_1to10'
    """
    name = did.split(':', 1)[-1].lstrip('/')
    parts = name.split('/')
    if len(parts) <= 3:
        return ''
    return '/'.join(parts[3:]).lower()


def _aggregate_rucio_match(matches):
    """Per-stage rollup over a list of matched dataset records.

    Rucio's /dids/<scope>/<name> endpoint returns length/bytes as null
    for these datasets; the canonical file count and byte size live on
    each RSE replica record (they all carry the same value since RSEs
    hold replicas of the same data). Take the max across RSEs.
    """
    by_stage = {}
    for m in matches:
        st = by_stage.setdefault(m['stage'], {
            'count': 0, 'files': 0, 'bytes': 0,
            'rses': {}, 'incomplete': 0,
        })
        st['count'] += 1
        ds_files = m.get('length') or 0
        ds_bytes = m.get('bytes')  or 0
        complete = True
        for r in m.get('rse_replicas', []):
            rse = r.get('rse')
            if not rse:
                continue
            ds_files = max(ds_files, r.get('length') or 0)
            ds_bytes = max(ds_bytes, r.get('bytes')  or 0)
            st['rses'].setdefault(rse, {'complete': 0, 'incomplete': 0})
            if r.get('available_length') == r.get('length') and r.get('length'):
                st['rses'][rse]['complete'] += 1
            else:
                st['rses'][rse]['incomplete'] += 1
                complete = False
        st['files'] += ds_files
        st['bytes'] += ds_bytes
        if not complete:
            st['incomplete'] += 1
    return by_stage


def _did_version(did):
    """Release version segment of a produced DID.

    ``epic:/RECO/26.04.1/epic_craterlake/...`` -> ``26.04.1`` (segment after
    the stage). Empty string if it can't be parsed.
    """
    name = (did or '').split(':', 1)[-1]
    parts = [p for p in name.split('/') if p]   # [stage, version, ...]
    return parts[1] if len(parts) > 1 else ''


def _rucio_match_to_output(m, checked_at=None):
    """Convert one produced Rucio dataset record into a unified ``outputs``
    entry (one per dataset, lifecycle-neutral) — see EPICPROD_DATA_LINEAGE.md.

    File count / byte size are taken as the max across RSE replicas (Rucio
    reports them per replica, identical across RSEs), matching
    ``_aggregate_rucio_match``.
    """
    did = m.get('did', '')
    files = m.get('length') or 0
    bytes_ = m.get('bytes') or 0
    rses = []
    complete = True
    for r in m.get('rse_replicas', []) or []:
        rse = r.get('rse')
        if not rse:
            continue
        total = r.get('length') or 0
        avail = r.get('available_length') or 0
        files = max(files, total)
        bytes_ = max(bytes_, r.get('bytes') or 0)
        rse_complete = bool(total) and avail == total
        complete = complete and rse_complete
        rses.append({'rse': rse, 'files': avail, 'total': total,
                     'complete': rse_complete})
    entry = {
        'did':        did,
        'stage':      m.get('stage', ''),
        'version':    _did_version(did),
        'filters':    _extract_past_filters(did),
        'rses':       rses,
        'file_count': files,
        'bytes':      bytes_,
        'complete':   complete if rses else False,
    }
    if checked_at:
        entry['checked_at'] = checked_at
    return entry


def _index_snapshot_by_tail(snapshot):
    """Pre-index a snapshot for fast tail-prefix lookup.

    Returns a list of (tail, dataset_record_with_stage) tuples sorted by
    tail length DESC, so the most-specific tails are tried first.
    """
    idx = []
    for cpath, info in snapshot.get('campaigns', {}).items():
        # cpath = '/RECO/26.02.0' or '/FULL/26.02.0'
        cp_parts = cpath.strip('/').split('/')
        stage = cp_parts[0] if cp_parts else ''
        for d in info.get('datasets', []):
            tail = _did_path_tail(d.get('did', ''))
            if not tail:
                continue
            idx.append((tail, {**d, 'stage': stage}))
    return idx


_Q2_MIN_RE   = _re.compile(r'^minQ2=([\d.]+)$', _re.I)
_Q2_RANGE_RE = _re.compile(r'^q2_([\d.]+)to([\d.]+)$', _re.I)
_Q2_POINT_RE = _re.compile(r'^q2_([\d.]+)$', _re.I)


def _q2_range(s):
    """Convert a Q² label into a (lo, hi) numeric range.

    'minQ2=1'    -> (1, inf)
    'q2_1to10'   -> (1, 10)
    'q2_20'      -> (20, 20)
    anything else / empty -> None
    """
    if not s:
        return None
    m = _Q2_MIN_RE.match(s)
    if m:
        return (float(m.group(1)), float('inf'))
    m = _Q2_RANGE_RE.match(s)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = _Q2_POINT_RE.match(s)
    if m:
        v = float(m.group(1))
        return (v, v)
    return None


def _q2_overlap(req_q2, did_q2):
    """True if request and DID Q² labels could plausibly intersect."""
    if not req_q2 or not did_q2:
        return True   # absent on either side: don't block the match
    a = _q2_range(req_q2)
    b = _q2_range(did_q2)
    if a is None or b is None:
        return req_q2 == did_q2  # exact string fallback
    return a[0] <= b[1] and b[0] <= a[1]


def _filter_match(req, did):
    """True if a request's filter fields match an output DID's.

    Matches on the semantic axes shared by both schemas (detector,
    beam, physics, Q²) rather than on path strings — the output DID
    carries extra segments (generator, radiation, charge) and a
    different Q² spelling that defeat string matching. See
    project-pcs-request-vs-output-paths.
    """
    for k in ('detector', 'beam', 'physics'):
        rv, dv = req.get(k), did.get(k)
        if rv and dv and rv != dv:
            return False
        if rv and not dv:
            return False
    if not _q2_overlap(req.get('q2'), did.get('q2')):
        return False
    # Species/energy only meaningful for SINGLE-physics paths; require
    # both sides to agree when both populated.
    for k in ('species', 'energy'):
        rv, dv = req.get(k), did.get(k)
        if rv and dv and rv != dv:
            return False
    return True


def _did_tail_segments(did_tail):
    return did_tail.split('/') if did_tail else []


def _path_aligned_match(did_segs, req_segs):
    """Is `req_segs` a contiguous subsequence of `did_segs`?

    Rucio output paths often carry a background-mixing chain prefix
    (e.g. Bkg_Exact1S_2us/GoldCt/5um/) AND a Q²-bin suffix that the
    CSV input tail doesn't carry. Subsequence matching handles both.
    """
    if not req_segs or len(req_segs) > len(did_segs):
        return False
    n = len(req_segs)
    for i in range(len(did_segs) - n + 1):
        if did_segs[i:i + n] == req_segs:
            return True
    return False


def match_requests_to_rucio_snapshot(snapshot, *, campaign):
    """For every ProdTask in `campaign` whose CSV input has a usable tail,
    stash an `overrides['csv_import']['output']` rollup of matching Rucio
    datasets. A Rucio dataset matches when its DID-name tail
    (after /STAGE/<v>/<detector>/) contains the request input tail as a
    contiguous, path-aligned segment sequence — so BG-mixing prefixes
    and Q²-bin suffixes in the output don't break the match.

    Also stashes the unmatched Rucio datasets on
    ``campaign.data['rucio_unmatched']`` so the catalog view can surface
    them as synthetic table rows (unmatched output popping up).
    """
    # Precompute (rec, did_filters) so the matcher loop is O(req * did).
    idx = _index_snapshot_by_tail(snapshot)
    checked_at = snapshot.get('fetched_at')
    idx_filtered = [(rec, _extract_past_filters(rec['did']))
                    for _tail, rec in idx]
    qs = ProdTask.objects.filter(campaign=campaign).select_related('dataset')
    summary = {'tasks_seen': 0, 'tasks_matched': 0, 'tasks_unmatched': 0}
    matched_dids = set()
    for t in qs:
        summary['tasks_seen'] += 1
        # Prefer the persisted csv_import.filters block (already extracted
        # at ingest time) and fall back to a fresh extract from the CSV
        # input path for ProdTasks that predate the filter ingest.
        req_filters = ((t.overrides or {}).get('csv_import') or {}).get('filters') or {}
        if not req_filters:
            ds_path = (t.dataset.metadata or {}).get('source', {}).get('location', '') \
                if (t.dataset.metadata or {}) else ''
            req_filters = _extract_csv_filters(ds_path, t.dataset.detector_config) \
                if ds_path else {}
        matches = []
        if req_filters and any(req_filters.get(k) for k in ('beam', 'physics')):
            for rec, did_filters in idx_filtered:
                if _filter_match(req_filters, did_filters):
                    matches.append(rec)
        for m in matches:
            matched_dids.add(m['did'])
        overrides = dict(t.overrides or {})
        overrides['outputs'] = [_rucio_match_to_output(m, checked_at) for m in matches]
        # Drop the superseded aggregate if present — outputs is now the home.
        cv = overrides.get('csv_import')
        if isinstance(cv, dict) and 'output' in cv:
            cv = dict(cv)
            cv.pop('output', None)
            overrides['csv_import'] = cv
        t.overrides = overrides
        t.save(update_fields=['overrides', 'updated_at'])
        if matches:
            summary['tasks_matched'] += 1
        else:
            summary['tasks_unmatched'] += 1

    # Unmatched Rucio datasets: in the snapshot but no request matched.
    # Light-weight records — just the fields the catalog row needs.
    unmatched = []
    for cpath, info in (snapshot.get('campaigns') or {}).items():
        cp_parts = cpath.strip('/').split('/')
        stage = cp_parts[0] if cp_parts else ''
        for d in info.get('datasets') or []:
            did = d.get('did', '')
            if did and did not in matched_dids:
                # Aggregate to a single-stage rollup of the SAME shape used
                # for matched-request output so the template can reuse the
                # output-line rendering.
                rollup = _aggregate_rucio_match([{**d, 'stage': stage}])
                files = sum(s['files'] for s in rollup.values())
                bytes_ = sum(s['bytes'] for s in rollup.values())
                any_incomplete = any(s.get('incomplete', 0) for s in rollup.values())
                unmatched.append({
                    'did':        did,
                    'stage':      stage,
                    'files':      files,
                    'bytes':      bytes_,
                    'rse_names':  sorted({r.get('rse', '') for r in d.get('rse_replicas', []) if r.get('rse')}),
                    'by_stage':   rollup,
                    'incomplete': any_incomplete,
                    'filters':    _extract_past_filters(did),
                })
    campaign.data = {**(campaign.data or {}), 'rucio_unmatched': unmatched}
    campaign.save(update_fields=['data', 'updated_at'])
    summary['rucio_unmatched'] = len(unmatched)
    return summary


def migrate_outputs_schema(*, apply=False):
    """One-time migration onto the unified ``ProdTask.overrides['outputs']``
    schema — one entry per produced Rucio dataset, lifecycle-neutral
    (EPICPROD_DATA_LINEAGE.md).

    - ``past_output`` tasks: reshape ``overrides['past_output']`` + the
      Dataset's Rucio DID/counts into one ``outputs`` entry; drop the
      ``past_output`` key.
    - tasks carrying the old ``csv_import.output`` rollup: drop that aggregate
      (explicit DIDs are re-populated by the next Rucio match).

    ``apply=False`` (default) is a dry run — counts only, no writes. Per-task
    errors are collected, never swallowed.
    """
    summary = {'seen': 0, 'past_migrated': 0, 'aggregate_dropped': 0,
               'errors': []}
    for t in ProdTask.objects.select_related('dataset').iterator():
        try:
            ov = dict(t.overrides or {})
            changed = False
            po = ov.get('past_output')
            if po and not ov.get('outputs'):
                md = (t.dataset.metadata or {}) if t.dataset else {}
                did = (md.get('source') or {}).get('location', '') if md else ''
                rses = [{'rse': r.get('name'), 'files': r.get('files'),
                         'total': r.get('total'),
                         'complete': r.get('status') == 'complete'}
                        for r in (po.get('rses') or [])]
                ov['outputs'] = [{
                    'did':        did,
                    'stage':      po.get('stage', ''),
                    'version':    po.get('version', ''),
                    'filters':    po.get('filters', {}),
                    'rses':       rses,
                    'file_count': t.dataset.file_count if t.dataset else 0,
                    'bytes':      t.dataset.data_size if t.dataset else 0,
                    'complete':   po.get('complete', True),
                }]
                summary['past_migrated'] += 1
                changed = True
            if 'past_output' in ov:
                ov.pop('past_output', None)
                changed = True
            cv = ov.get('csv_import')
            if isinstance(cv, dict) and 'output' in cv:
                cv = dict(cv)
                cv.pop('output', None)
                ov['csv_import'] = cv
                summary['aggregate_dropped'] += 1
                changed = True
            summary['seen'] += 1
            if changed and apply:
                t.overrides = ov
                t.save(update_fields=['overrides', 'updated_at'])
        except Exception as e:                                # noqa: BLE001
            summary['errors'].append(f'{t.name}: {e}')
    return summary


def summarize_rucio_timeline(snapshot, *, bin_hours=12):
    """Build a per-bin cumulative arrival timeline from a Rucio snapshot.

    'Arrival' = the earliest created_at across all RSE replicas of a
    dataset. Datasets without any usable timestamp are dropped. Each
    arrival lands in the nearest `bin_hours`-wide bin (default 12h,
    aligned to UTC midnight). Returns a dict suitable for Plotly:

        {'dates': ['YYYY-MM-DDTHH:00:00', ...],
         'bin_hours': 12,
         'simu': {'cum_datasets':[...], 'cum_files':[...], 'cum_bytes':[...]},
         'reco': {'cum_datasets':[...], 'cum_files':[...], 'cum_bytes':[...]}}
    """
    from email.utils import parsedate_to_datetime as _pd
    import datetime as _dt
    bin_size = _dt.timedelta(hours=bin_hours)

    def _bucket(dt):
        """Floor `dt` to the nearest `bin_hours`-bin aligned to UTC midnight."""
        midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        offset = (dt - midnight).total_seconds()
        return midnight + _dt.timedelta(
            seconds=(int(offset) // int(bin_size.total_seconds()))
                    * int(bin_size.total_seconds()))

    arrivals = []  # (bucket_iso, stage, files, bytes)
    for cpath, info in (snapshot.get('campaigns') or {}).items():
        cp_parts = cpath.strip('/').split('/')
        stage = cp_parts[0] if cp_parts else ''
        for d in info.get('datasets') or []:
            rses = d.get('rse_replicas') or []
            tsps = []
            files = bytes_ = 0
            for r in rses:
                ts_str = r.get('created_at')
                if ts_str:
                    try:
                        tsps.append(_pd(ts_str))
                    except Exception:                         # noqa: BLE001
                        pass
                files = max(files, r.get('length') or 0)
                bytes_ = max(bytes_, r.get('bytes')  or 0)
            if not tsps:
                continue
            arrivals.append((_bucket(min(tsps)).strftime('%Y-%m-%dT%H:%M:%S'),
                             stage, files, bytes_))
    arrivals.sort()
    if not arrivals:
        return {'dates': [], 'bin_hours': bin_hours, 'simu': {}, 'reco': {}}

    first = _dt.datetime.fromisoformat(arrivals[0][0])
    last_arr = _dt.datetime.fromisoformat(arrivals[-1][0])
    # Extend the axis to the current bin (UTC) so a quiet stretch
    # between the last arrival and now shows as a flat segment.
    # Don't go past 'now' — no empty future bins on the axis.
    last = max(last_arr, _bucket(_dt.datetime.utcnow()))
    span = last - first
    n_bins = int(span.total_seconds() // bin_size.total_seconds()) + 1
    dates = [(first + i * bin_size).strftime('%Y-%m-%dT%H:%M:%S')
             for i in range(n_bins)]
    idx = {d: i for i, d in enumerate(dates)}

    def _empty():
        return {'cum_datasets': [0]*n_bins, 'cum_files': [0]*n_bins, 'cum_bytes': [0]*n_bins}
    out = {'dates': dates, 'bin_hours': bin_hours, 'simu': _empty(), 'reco': _empty()}
    per_bin = {'FULL': _empty(), 'RECO': _empty()}
    for d, stage, files, bytes_ in arrivals:
        if stage not in per_bin or d not in idx:
            continue
        i = idx[d]
        per_bin[stage]['cum_datasets'][i] += 1
        per_bin[stage]['cum_files'][i]    += files
        per_bin[stage]['cum_bytes'][i]    += bytes_
    for stage, key in (('FULL', 'simu'), ('RECO', 'reco')):
        cd = cf = cb = 0
        for i in range(n_bins):
            cd += per_bin[stage]['cum_datasets'][i]
            cf += per_bin[stage]['cum_files'][i]
            cb += per_bin[stage]['cum_bytes'][i]
            out[key]['cum_datasets'][i] = cd
            out[key]['cum_files'][i]    = cf
            out[key]['cum_bytes'][i]    = cb
    return out


def _detect_active_releases(token=None, *, year_prefix='26.'):
    """Per-release dataset counts under epic:/RECO/<v>/ in JLab Rucio.

    Returns list of {version, count} for every <year_prefix>x.y release
    that has at least one dataset, sorted newest-first by component
    version. 'main' excluded. Trial runs that land a handful of
    datasets before real production starts are deliberately NOT
    filtered out here — the operator judges from the counts, and
    nothing is auto-promoted. Humans switch (see
    feedback-humans-switch-lifecycle).
    """
    if token is None:
        token = _jlab_rucio_auth()
    names = _ndjson(_jlab_rucio_get(
        '/dids/epic/dids/search', token, type='dataset', name='/RECO/*'))
    from collections import Counter as _Counter
    counts = _Counter()
    for n in names:
        if not isinstance(n, str):
            continue
        parts = n.lstrip('/').split('/')
        if len(parts) < 2:
            continue
        v = parts[1]
        if v == 'main' or not v.startswith(year_prefix):
            continue
        counts[v] += 1
    def _key(v):
        out = []
        for part in v.split('.'):
            try:
                out.append((0, int(part)))
            except ValueError:
                out.append((1, part))
        return tuple(out)
    return [{'version': v, 'count': counts[v]}
            for v in sorted(counts, key=_key, reverse=True)]


def load_rucio_snapshot(campaign_name, *, snapshot_dir=RUCIO_SNAPSHOT_DIR):
    """Read a saved JLab Rucio snapshot. Returns None if absent."""
    import json as _json
    import os as _os
    path = _os.path.join(snapshot_dir, f'current-{campaign_name}.json')
    if not _os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return _json.load(f)
    except (OSError, _json.JSONDecodeError) as e:
        _log.warning('load_rucio_snapshot %s: %s', path, e)
        return None


def import_jlab_rucio_current_snapshot(*, campaign_name=None,
                                      snapshot_dir=RUCIO_SNAPSHOT_DIR,
                                      created_by='rucio_snapshot'):
    """Pull the JLab Rucio snapshot for the PCS current campaign and save
    it under the snapshot directory.

    campaign_name: '26.02.0' (or override). If None, uses the
        lifecycle='current' Campaign. Both /RECO/<v> and /FULL/<v> are
        fetched.
    snapshot_dir: writable directory. One JSON file per current campaign:
        '<snapshot_dir>/current-<campaign>.json'.
    NO-SILENT-FAILURES: every network / parse error is collected into
    summary['errors'] and surfaced to the operator.
    """
    import json as _json
    import os as _os
    import time as _time

    summary = {'campaign': '', 'paths': {}, 'errors': [], 'snapshot_path': ''}

    # If no name given, default to lifecycle='current'. Callers may pass
    # a 'last' campaign name explicitly.
    if campaign_name is None:
        current = (Campaign.objects.filter(lifecycle='current')
                   .order_by('-updated_at').first())
        if not current:
            raise ServiceError('No current Campaign defined in PCS')
        campaign_name = current.name
    summary['campaign'] = campaign_name

    _os.makedirs(snapshot_dir, exist_ok=True)
    out_path = _os.path.join(snapshot_dir, f'current-{campaign_name}.json')
    summary['snapshot_path'] = out_path

    try:
        token = _jlab_rucio_auth()
    except Exception as e:                                    # noqa: BLE001
        raise ServiceError(f'JLab Rucio auth failed: {e}')

    snapshot = {
        'fetched_at':    _time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'scope':         'epic',
        'campaign_name': campaign_name,
        'campaigns':     {},
    }
    for stage in ('RECO', 'FULL'):
        cpath = f'/{stage}/{campaign_name}'
        try:
            snapshot['campaigns'][cpath] = fetch_jlab_rucio_campaign(
                cpath, token=token)
            summary['paths'][cpath] = snapshot['campaigns'][cpath]['count']
        except Exception as e:                                # noqa: BLE001
            summary['errors'].append(f'{cpath}: {e}')
            snapshot['campaigns'][cpath] = {'count': 0, 'datasets': [],
                                            'error': str(e)}
            summary['paths'][cpath] = 0

    with open(out_path, 'w') as f:
        _json.dump(snapshot, f, indent=2)
    summary['file_bytes'] = _os.path.getsize(out_path)

    # Detect active 26.x releases in JLab Rucio + stash on the PCS
    # current Campaign so the catalog can surface a 'Switch current'
    # banner. AI proposes; human switches (feedback-humans-switch-
    # lifecycle).
    try:
        token = _jlab_rucio_auth()
        detected = _detect_active_releases(token=token)
    except Exception as e:                                    # noqa: BLE001
        summary['errors'].append(f'detect_active_releases: {e}')
        detected = []
    summary['detected_releases'] = detected

    # Cache the per-request Output rollup on each ProdTask in this
    # campaign so the Current tab can render it without re-reading
    # the snapshot file on every page load.
    try:
        camp = Campaign.objects.get(name=campaign_name)
        # Stash the detected-releases list on the Campaign for the
        # banner; rename / promotion is a separate operator action.
        camp.data = {**(camp.data or {}), 'detected_releases': detected}
        camp.save(update_fields=['data', 'updated_at'])
        # Match the request set to the Rucio snapshot only for the
        # 'current' campaign — the 'last' campaign has no linked
        # request rows (CSV requests are bound to current), so there's
        # nothing to roll up.
        if camp.lifecycle == 'current':
            match_summary = match_requests_to_rucio_snapshot(snapshot, campaign=camp)
            summary['match'] = match_summary
    except Campaign.DoesNotExist:
        summary['errors'].append(
            f"campaign '{campaign_name}' not in PCS - skipping match step")
    except Exception as e:                                    # noqa: BLE001
        summary['errors'].append(f'request match step failed: {e}')

    return summary


def refresh_rucio_snapshots(*, created_by='rucio_snapshot'):
    """Pull the JLab Rucio snapshot for the current (+last) PCS campaign(s) and
    rematch produced datasets onto each task's ``overrides['outputs']``.

    The work behind the catalog's "Update from Rucio" button. Slow and
    network-bound (a live JLab Rucio fetch of /RECO + /FULL plus the match over
    every task), so it runs off the web request — in the prod-ops agent's
    ``rucio_snapshot_update`` doer. Returns ``{'summaries': [...], 'errors':
    [...]}``; errors are collected per campaign, never swallowed.
    """
    targets = list(Campaign.objects.filter(lifecycle__in=['current', 'last'])
                   .order_by('lifecycle'))
    out = {'summaries': [], 'errors': []}
    if not targets:
        out['errors'].append('No current Campaign defined in PCS')
        return out
    for camp in targets:
        try:
            out['summaries'].append(import_jlab_rucio_current_snapshot(
                campaign_name=camp.name, created_by=created_by))
        except Exception as e:                                # noqa: BLE001
            out['errors'].append(f'{camp.lifecycle} {camp.name}: {e}')
    return out


def set_pcs_campaign_lifecycle(new_name, target_lifecycle, *, created_by='operator'):
    """Set the PCS Campaign with lifecycle=`target_lifecycle` to `new_name`.

    target_lifecycle is 'current' or 'last' (singular slots — at most one
    Campaign at a time). If the slot is occupied, the existing occupant
    is renamed in place; ProdTask FKs are preserved (same row mutated).
    If empty, a new Campaign(lifecycle=target_lifecycle) is created.
    Operator-initiated only — never call from a sync / refresh handler
    (see feedback-humans-switch-lifecycle).
    """
    if not new_name:
        raise ServiceError('set_pcs_campaign_lifecycle: empty target name')
    if target_lifecycle not in ('current', 'last'):
        raise ServiceError(f'unsupported lifecycle {target_lifecycle!r}')
    existing = (Campaign.objects.filter(lifecycle=target_lifecycle)
                .order_by('-updated_at').first())
    if existing is not None:
        if existing.name == new_name:
            return {'changed': False, 'name': new_name, 'lifecycle': target_lifecycle}
        if Campaign.objects.filter(name=new_name).exclude(pk=existing.pk).exists():
            raise ServiceError(
                f'Campaign named {new_name!r} already exists; '
                f'cannot rename {existing.name!r} into it')
        old_name = existing.name
        existing.name = new_name
        existing.save(update_fields=['name', 'updated_at'])
        _log.info('PCS %s campaign renamed: %s -> %s (by %s)',
                  target_lifecycle, old_name, new_name, created_by)
        return {'changed': True, 'old_name': old_name, 'name': new_name,
                'lifecycle': target_lifecycle, 'created': False}
    # No existing slot — create one. Refuse if any Campaign already uses
    # this name (avoid hijacking past 'FULL/26.04.1' / 'RECO/26.04.1').
    if Campaign.objects.filter(name=new_name).exists():
        raise ServiceError(
            f'Campaign named {new_name!r} already exists; '
            f'cannot create a new {target_lifecycle} with that name')
    Campaign.objects.create(name=new_name, lifecycle=target_lifecycle,
                            created_by=created_by)
    _log.info('PCS %s campaign created: %s (by %s)',
              target_lifecycle, new_name, created_by)
    return {'changed': True, 'old_name': None, 'name': new_name,
            'lifecycle': target_lifecycle, 'created': True}


# Backwards-compat wrapper.
def rename_pcs_current_campaign(new_name, *, created_by='operator'):
    return set_pcs_campaign_lifecycle(new_name, 'current', created_by=created_by)


def prodtask_record_submission(*, task, jedi_task_id, new_status='submitted'):
    """
    Record outcome of a JEDI submission.

    - **Idempotent on the jediTaskID.** Re-recording the SAME id is a no-op
      success — a doer retry, or a manual re-record after an orphaned
      submission whose record-back POST failed. A DIFFERENT id on an
      already-recorded task is refused (409).
    - Otherwise the task must be in status 'ready' (no record from draft).
    """
    try:
        incoming = int(jedi_task_id)
    except (TypeError, ValueError):
        raise ServiceError('jedi_task_id must be an integer')

    if task.panda_task_id is not None:
        if task.panda_task_id == incoming:
            return task          # already recorded this submission — idempotent
        raise ServiceError(
            f'Task already records panda_task_id={task.panda_task_id}, '
            f'cannot overwrite with {incoming}.',
            status=409,
        )

    # Commissioning relaxation: recording a submission from a draft task is
    # allowed — the 'ready' freeze is not required. See
    # docs/COMMISSIONING_RELAXATIONS.md.

    valid = _known_prodtask_statuses()
    if new_status not in valid:
        raise ServiceError(
            f'Invalid status. Choose from: {", ".join(sorted(valid))}'
        )
    task.panda_task_id = incoming
    task.status = new_status
    task.save(update_fields=['panda_task_id', 'status', 'updated_at'])
    return task


def prodtask_submit_request(*, task):
    """Publish a submit_task request for a locked (ready) task to the prod-ops
    agent. The web tier holds no PanDA credential — it only asks the agent to
    run the submission, which records the jediTaskID back. Gates mirror
    prodtask_record_submission so we never fire a submission whose outcome
    would then be refused. Raises ServiceError on a bad state or an
    unreachable queue. This is the single submit trigger, behind the REST
    `submit` action (compose view + the task detail page's "Submit in Compose"
    link); the legacy page-view submit was retired."""
    import json as _json
    if task.panda_task_id is not None:
        raise ServiceError(
            f'Already submitted as jediTaskID {task.panda_task_id}.', status=409)
    # Commissioning relaxation: a draft task may be submitted directly — the
    # 'ready' freeze is not required. Readiness is surfaced as a non-blocking
    # warning by the caller, not gated here. See docs/COMMISSIONING_RELAXATIONS.md.
    msg = {'msg_type': 'submit_task', 'namespace': 'prodops',
           'task_name': task.name, 'owner': task.created_by}
    from monitor_app.activemq_connection import ActiveMQConnectionManager
    try:
        triggered = ActiveMQConnectionManager().send_message(
            '/queue/epicprod.ops', _json.dumps(msg))
    except Exception as e:
        raise ServiceError(f'Could not reach the prod-ops agent queue: {e}', status=503)
    if not triggered:
        raise ServiceError(
            'Submission could not be queued (ops-agent queue unreachable).', status=503)
    return task


def rucio_snapshot_update_request(*, created_by='rucio_snapshot'):
    """Publish a rucio_snapshot_update request to the prod-ops agent, which
    refreshes the JLab Rucio snapshot for the current (+last) campaign and
    rematches produced datasets onto each task's overrides['outputs'] in the
    background, then pushes rucio_snapshot_ready over the SSE relay. The live
    JLab fetch + per-task match is far too slow to run inline in a web request,
    so the web tier only drops the message. Requires a current or last Campaign.
    Raises ServiceError on a bad state or an unreachable queue. See
    docs/EPICPROD_DATA_LINEAGE.md, docs/EPICPROD_OPS_AGENT.md."""
    import json as _json
    from .models import Campaign
    if not Campaign.objects.filter(lifecycle__in=['current', 'last']).exists():
        raise ServiceError('No current Campaign defined in PCS.', status=400)
    msg = {'msg_type': 'rucio_snapshot_update', 'namespace': 'prodops',
           'created_by': created_by}
    from monitor_app.activemq_connection import ActiveMQConnectionManager
    try:
        triggered = ActiveMQConnectionManager().send_message(
            '/queue/epicprod.ops', _json.dumps(msg))
    except Exception as e:
        raise ServiceError(
            f'Could not reach the prod-ops agent queue: {e}', status=503)
    if not triggered:
        raise ServiceError(
            'Rucio update could not be queued (ops-agent queue unreachable).',
            status=503)


def catalog_import_request(source, *, created_by='catalog_import'):
    """Publish a catalog import request to the prod-ops agent, which runs the
    import in the background and pushes catalog_import_ready over the SSE relay.
    The epic-prod past import walks ~4900 datasets — too slow to run inline in a
    web request (it times the gateway out), so the web tier only drops the
    message. ``source`` is 'csv' (the current default_datasets.csv) or
    'epic-prod' (the past/future FULL+RECO output). Raises ServiceError on a bad
    source or an unreachable queue. See docs/EPICPROD_OPS_AGENT.md, SSE_PUSH.md."""
    import json as _json
    if source not in ('csv', 'epic-prod'):
        raise ServiceError(f'Unknown catalog import source {source!r}.', status=400)
    msg = {'msg_type': 'catalog_import', 'source': source,
           'namespace': 'prodops', 'created_by': created_by}
    from monitor_app.activemq_connection import ActiveMQConnectionManager
    try:
        triggered = ActiveMQConnectionManager().send_message(
            '/queue/epicprod.ops', _json.dumps(msg))
    except Exception as e:
        raise ServiceError(
            f'Could not reach the prod-ops agent queue: {e}', status=503)
    if not triggered:
        raise ServiceError(
            'Catalog import could not be queued (ops-agent queue unreachable).',
            status=503)
