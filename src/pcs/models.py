"""
PCS (Physics Configuration System) data models.

Tag lifecycle: draft (editable) → locked (immutable, usable in datasets).
Tag numbering: physics tags = category.digit * 1000 + N; e/s/r tags increment from 1 via PersistentState.
Datasets: composed from four locked tags, auto-named, with block management for Rucio's 100k file limit.
"""
from django.db import models, transaction
from django.core.validators import MinValueValidator, MaxValueValidator
from django.core.exceptions import ValidationError


TAG_STATUS_CHOICES = [
    ('draft', 'Draft'),
    ('locked', 'Locked'),
]


class PhysicsCategory(models.Model):
    """Physics area (e.g. DVCS=3, DIS=4). Digit determines tag number range."""
    digit = models.PositiveSmallIntegerField(
        primary_key=True,
        validators=[MinValueValidator(1), MaxValueValidator(9)],
        help_text="Single digit 1-9. Physics tag numbers = digit * 1000 + N."
    )
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, default='')
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pcs_physics_category'
        ordering = ['digit']
        verbose_name_plural = 'physics categories'

    def __str__(self):
        return f"{self.digit}: {self.name}"


class PhysicsTag(models.Model):
    """Physics process tag (p3001, p3002...). Number = category.digit * 1000 + N."""
    tag_number = models.IntegerField(unique=True)
    tag_label = models.CharField(max_length=10, unique=True)
    category = models.ForeignKey(
        PhysicsCategory, on_delete=models.PROTECT, related_name='tags'
    )
    status = models.CharField(max_length=10, choices=TAG_STATUS_CHOICES, default='draft')
    description = models.TextField(blank=True, default='')
    parameters = models.JSONField(default=dict)
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_physics_tag'
        ordering = ['tag_number']

    def __str__(self):
        return self.tag_label

    def save(self, *args, **kwargs):
        self.tag_label = f"p{self.tag_number}"
        super().save(*args, **kwargs)

    @classmethod
    def allocate_next(cls, category):
        """Atomically allocate the next tag number: category.digit * 1000 + global suffix."""
        suffix = _allocate_simple_tag('pcs_next_physics')
        return category.digit * 1000 + suffix


class EvgenTag(models.Model):
    """Event generation tag (e1, e2...). Number auto-incremented via PersistentState."""
    tag_number = models.IntegerField(unique=True)
    tag_label = models.CharField(max_length=10, unique=True)
    status = models.CharField(max_length=10, choices=TAG_STATUS_CHOICES, default='draft')
    description = models.TextField(blank=True, default='')
    parameters = models.JSONField(default=dict)
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_evgen_tag'
        ordering = ['tag_number']

    def __str__(self):
        return self.tag_label

    def save(self, *args, **kwargs):
        self.tag_label = f"e{self.tag_number}"
        super().save(*args, **kwargs)

    @classmethod
    def allocate_next(cls):
        return _allocate_simple_tag('pcs_next_evgen')


class SimuTag(models.Model):
    """Simulation tag (s1, s2...). Number auto-incremented via PersistentState."""
    tag_number = models.IntegerField(unique=True)
    tag_label = models.CharField(max_length=10, unique=True)
    status = models.CharField(max_length=10, choices=TAG_STATUS_CHOICES, default='draft')
    description = models.TextField(blank=True, default='')
    parameters = models.JSONField(default=dict)
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_simu_tag'
        ordering = ['tag_number']

    def __str__(self):
        return self.tag_label

    def save(self, *args, **kwargs):
        self.tag_label = f"s{self.tag_number}"
        super().save(*args, **kwargs)

    @classmethod
    def allocate_next(cls):
        return _allocate_simple_tag('pcs_next_simu')


class RecoTag(models.Model):
    """Reconstruction tag (r1, r2...). Number auto-incremented via PersistentState."""
    tag_number = models.IntegerField(unique=True)
    tag_label = models.CharField(max_length=10, unique=True)
    status = models.CharField(max_length=10, choices=TAG_STATUS_CHOICES, default='draft')
    description = models.TextField(blank=True, default='')
    parameters = models.JSONField(default=dict)
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_reco_tag'
        ordering = ['tag_number']

    def __str__(self):
        return self.tag_label

    def save(self, *args, **kwargs):
        self.tag_label = f"r{self.tag_number}"
        super().save(*args, **kwargs)

    @classmethod
    def allocate_next(cls):
        return _allocate_simple_tag('pcs_next_reco')


class BackgroundTag(models.Model):
    """Background tag (k1, k2...). A named, versioned background configuration
    (beam-gas, synchrotron radiation, or overlay samples), independent of any
    physics signal. Number auto-incremented via PersistentState."""
    tag_number = models.IntegerField(unique=True)
    tag_label = models.CharField(max_length=10, unique=True)
    status = models.CharField(max_length=10, choices=TAG_STATUS_CHOICES, default='draft')
    description = models.TextField(blank=True, default='')
    parameters = models.JSONField(default=dict)
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_background_tag'
        ordering = ['tag_number']

    def __str__(self):
        return self.tag_label

    def save(self, *args, **kwargs):
        self.tag_label = f"k{self.tag_number}"
        super().save(*args, **kwargs)

    @classmethod
    def allocate_next(cls):
        return _allocate_simple_tag('pcs_next_background')


class Dataset(models.Model):
    """
    Production dataset composed from four locked tags.

    Each row is one block. Block 1 always exists. The dataset_name ties
    blocks together. The DID format is '{scope}:{dataset_name}.b{N}'.
    All tags must be locked before a dataset can be created.
    """
    dataset_name = models.CharField(max_length=255)
    scope = models.CharField(max_length=100, default='group.EIC')
    detector_version = models.CharField(max_length=50)
    detector_config = models.CharField(max_length=100)
    physics_tag = models.ForeignKey(PhysicsTag, on_delete=models.PROTECT, related_name='datasets')
    evgen_tag = models.ForeignKey(EvgenTag, on_delete=models.PROTECT, related_name='datasets')
    simu_tag = models.ForeignKey(SimuTag, on_delete=models.PROTECT, related_name='datasets')
    reco_tag = models.ForeignKey(RecoTag, on_delete=models.PROTECT, related_name='datasets')
    background_tag = models.ForeignKey(
        BackgroundTag, on_delete=models.PROTECT, related_name='datasets',
        null=True, blank=True,
    )
    block_num = models.PositiveIntegerField(default=1)
    blocks = models.PositiveIntegerField(default=1)
    did = models.CharField(max_length=300, unique=True)
    file_count = models.IntegerField(default=0)
    data_size = models.BigIntegerField(default=0)
    description = models.TextField(blank=True, default='')
    metadata = models.JSONField(null=True, blank=True)
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    EXTERNAL_SOURCE_KINDS = {'csv_manifest', 'path', 'url', 'rucio_did', 'file_list'}

    class Meta:
        db_table = 'pcs_dataset'
        ordering = ['-created_at']
        unique_together = [('dataset_name', 'block_num')]

    def __str__(self):
        return self.did

    def get_metadata(self):
        """Return metadata as a mutable dict, normalizing empty/null values."""
        return self.metadata if isinstance(self.metadata, dict) else {}

    def get_metadata_value(self, *path, default=None):
        data = self.get_metadata()
        for key in path:
            if not isinstance(data, dict) or key not in data:
                return default
            data = data[key]
        return data

    @property
    def stage(self):
        return self.get_metadata_value('stage', default='')

    @property
    def is_external(self):
        source_kind = self.source_kind
        if source_kind:
            return source_kind in self.EXTERNAL_SOURCE_KINDS
        return bool(self.get_metadata_value('external', default=False))

    @property
    def source_kind(self):
        return self.get_metadata_value('source', 'kind', default='')

    @property
    def source_location(self):
        return self.get_metadata_value('source', 'location', default='')

    @property
    def validation_status(self):
        return self.get_metadata_value('validation', 'status', default='')

    @classmethod
    def external_evgen_metadata(
        cls, source_location, source_kind='csv_manifest', **_ignored
    ):
        return {
            'stage': 'evgen',
            'source': {
                'kind': source_kind,
                'location': source_location,
            },
        }

    def clean(self):
        for tag_field in ['physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag']:
            tag = getattr(self, tag_field, None)
            if tag and tag.status != 'locked':
                raise ValidationError(
                    {tag_field: f"Tag {tag.tag_label} must be locked before use in a dataset."}
                )

    def save(self, *args, **kwargs):
        if not self.dataset_name:
            self.dataset_name = self.build_dataset_name()
        if not self.did:
            self.did = f"{self.scope}:{self.dataset_name}.b{self.block_num}"
        if len(self.dataset_name) > 255:
            raise ValidationError("Dataset name exceeds 255 characters.")
        self.full_clean()
        super().save(*args, **kwargs)

    def build_dataset_name(self):
        """Auto-name: {scope}.{detector_version}.{detector_config}.{p}.{e}.{s}.{r}[.{k}]

        The background segment is appended only when the dataset carries a
        background tag.
        """
        name = (
            f"{self.scope}.{self.detector_version}.{self.detector_config}"
            f".{self.physics_tag.tag_label}.{self.evgen_tag.tag_label}"
            f".{self.simu_tag.tag_label}.{self.reco_tag.tag_label}"
        )
        if self.background_tag_id:
            name = f"{name}.{self.background_tag.tag_label}"
        return name

    @property
    def task_name(self):
        """Task name = dataset_name (without .bN block suffix)."""
        return self.dataset_name


class ProdConfig(models.Model):
    """
    Production configuration template — mutable operational settings for job submission.

    Captures everything needed to build a production submit command beyond what
    the four tags and dataset already define: background mixing, output control,
    software stack, resource targets, condor template, and PanDA/Rucio overrides.
    Always mutable — the PanDA task/job spec is the immutable record.
    """
    name = models.CharField(max_length=200, unique=True,
                            help_text="Human-readable config name, e.g. 'DVCS 10x100 standard'")
    description = models.TextField(blank=True, default='')

    # Background mixing
    bg_mixing = models.BooleanField(default=False)
    bg_cross_section = models.CharField(max_length=200, blank=True, default='')
    bg_evtgen_file = models.CharField(max_length=500, blank=True, default='')

    # Output file control
    copy_reco = models.BooleanField(default=True)
    copy_full = models.BooleanField(default=False)
    copy_log = models.BooleanField(default=True)
    use_rucio = models.BooleanField(default=True)

    # Software stack
    jug_xl_tag = models.CharField(max_length=100, blank=True, default='',
                                  help_text="e.g. 26.02.0-stable")
    container_image = models.CharField(max_length=500, blank=True, default='',
                                       help_text="Singularity/Apptainer image reference")

    # Resource targets
    target_hours_per_job = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True,
                                               help_text="Target walltime per job in hours")
    events_per_task = models.PositiveIntegerField(null=True, blank=True,
                                                  help_text="Total events for the task")

    # Condor template
    condor_template = models.TextField(blank=True, default='',
                                       help_text="HTCondor submission template")

    # PanDA overrides (nullable — PanDA decides defaults)
    panda_site = models.CharField(max_length=200, blank=True, default='')
    panda_queue = models.CharField(max_length=200, blank=True, default='')
    panda_working_group = models.CharField(max_length=100, blank=True, default='')
    panda_resource_type = models.CharField(max_length=100, blank=True, default='')

    # Rucio overrides (nullable)
    rucio_rse = models.CharField(max_length=200, blank=True, default='',
                                 help_text="Rucio Storage Element for output")
    rucio_replication_rules = models.JSONField(null=True, blank=True,
                                               help_text="Rucio replication rule definitions")

    # Extensible submission parameters (no migration needed for new keys).
    # Keys: workflow_mode, transformation, processing_type,
    # prod_source_label, vo, n_jobs, events_per_job, events_per_file,
    # files_per_job, corecount, no_build, skip_scout, exec_command, scope
    data = models.JSONField(null=True, blank=True,
                            help_text="Additional submission parameters (JSON)")

    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_prod_config'
        ordering = ['-updated_at']

    def __str__(self):
        return self.name

    @property
    def workflow_mode(self):
        """Production workflow mode: 'external_evgen' (default) or
        'internal_evgen'. Stored in ``data['workflow_mode']``; defaults
        to 'external_evgen' (current production reality)."""
        return (self.data or {}).get('workflow_mode', 'external_evgen')

    @property
    def submission_path(self):
        """Submission path: 'condor' or 'panda'. Stored in
        ``data['submission_path']``; defaults to 'condor' (current
        production submission path). Whether evgen is run in-house
        or read from input files is a separate axis (workflow_mode)
        and is independent of the submission path."""
        return (self.data or {}).get('submission_path', 'condor')


CAMPAIGN_LIFECYCLE_CHOICES = [
    ('past', 'Past'),
    ('current', 'Current'),
    ('future', 'Future'),
]


class Campaign(models.Model):
    """
    Production campaign — a time-ordered grouping of ProdTasks.

    Lifecycle drives the catalog tabs: past (grey), current (green),
    future (blue). ``set_current`` in the service layer enforces the
    'one current at a time' invariant; no DB constraint, to keep
    transitions painless.
    """
    name = models.CharField(max_length=100, unique=True,
                            help_text="Campaign name, e.g. '26.02.0'")
    lifecycle = models.CharField(max_length=20, default='future')
    description = models.TextField(blank=True, default='')
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    clone_of = models.ForeignKey('self', null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name='clones',
                                 help_text="Campaign this was cloned from")
    data = models.JSONField(default=dict, blank=True,
                            help_text="Flexible extension fields (no migration to add new keys)")
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_campaign'
        ordering = ['-start_date', '-created_at']

    def __str__(self):
        return self.name


PRODREQUEST_STATUS_CHOICES = [
    ('new', 'New'),
    ('review', 'Review'),
    ('blocked', 'Blocked'),
    ('ready', 'Ready'),
    ('linked', 'Linked'),
    ('closed', 'Closed'),
]


class ProdRequest(models.Model):
    """
    PWG/DSC production request — upstream of ProdTask.

    Captures the request spreadsheet fields, system fields for intake
    idempotency and traceability, and production-team triage state.
    Use flags and requestor are duplicated onto ProdTask at task
    creation; both rows are mutable independently after that.
    """
    # Requester-facing fields (request spreadsheet)
    requestor = models.CharField(max_length=100, blank=True, default='',
                                 help_text="PWG or DSC making the request")
    simu_path = models.CharField(max_length=500, blank=True, default='',
                                 help_text="Declared simulation/EVGEN input location")
    gen_config = models.TextField(blank=True, default='',
                                  help_text="Generator configuration text from the request")
    nevents = models.BigIntegerField(null=True, blank=True,
                                     help_text="Requested event count")
    background = models.CharField(max_length=200, blank=True, default='',
                                  help_text="Requested background condition")
    description = models.TextField(blank=True, default='')
    priority = models.IntegerField(null=True, blank=True,
                                   help_text="Requester or production priority")

    # Use flags
    pre_tdr_use = models.BooleanField(default=False)
    early_science_use = models.BooleanField(default=False)
    other_use = models.BooleanField(default=False)
    new_request = models.BooleanField(default=False)

    # System / traceability
    status = models.CharField(max_length=20, default='new')
    source_url = models.CharField(max_length=500, blank=True, default='',
                                  help_text="Source spreadsheet or form URL")
    source_row = models.CharField(max_length=100, blank=True, default='',
                                  help_text="Source row identifier for idempotent import")

    # Production-team triage
    input_status = models.CharField(max_length=100, blank=True, default='',
                                    help_text="Whether declared inputs are located/registered/usable")
    rucio_source = models.CharField(max_length=300, blank=True, default='',
                                    help_text="Rucio DID or container for registered input")
    validation_status = models.CharField(max_length=100, blank=True, default='',
                                         help_text="Validation summary")

    data = models.JSONField(default=dict, blank=True,
                            help_text="Flexible extension fields (no migration to add new keys)")
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_prod_request'
        ordering = ['-updated_at']

    def __str__(self):
        return f"req#{self.pk} {self.requestor or '(unspecified)'}"


PRODTASK_STATUS_CHOICES = [
    ('draft', 'Draft'),
    ('ready', 'Ready'),
    ('submitted', 'Submitted'),
    ('completed', 'Completed'),
    ('failed', 'Failed'),
]


class ProdTask(models.Model):
    """
    A production task: Dataset + ProdConfig + submission-specific params.
    Fully defines a production submission from which Condor and PanDA
    commands can be generated.
    """
    name = models.CharField(max_length=255, unique=True,
                            help_text="Task name (auto-derived from dataset or manual)")
    description = models.TextField(blank=True, default='')
    status = models.CharField(max_length=20, default='draft')

    # Core composition
    dataset = models.ForeignKey(Dataset, on_delete=models.PROTECT, related_name='prod_tasks')
    prod_config = models.ForeignKey(ProdConfig, on_delete=models.PROTECT, related_name='prod_tasks')

    # Campaign / Request linkage
    campaign = models.ForeignKey(Campaign, null=True, blank=True,
                                 on_delete=models.PROTECT, related_name='prod_tasks',
                                 help_text="Campaign this task belongs to")
    request = models.ForeignKey(ProdRequest, null=True, blank=True,
                                on_delete=models.SET_NULL, related_name='prod_tasks',
                                help_text="Originating PWG/DSC request, if any")

    # Catalog row fields (seeded from request at creation; mutable thereafter)
    requestor = models.CharField(max_length=100, blank=True, default='')
    priority = models.IntegerField(null=True, blank=True)
    pre_tdr_use = models.BooleanField(default=False)
    early_science_use = models.BooleanField(default=False)
    other_use = models.BooleanField(default=False)
    new_request = models.BooleanField(default=False)

    # Task-specific submission params
    csv_file = models.CharField(max_length=500, blank=True, default='',
                                help_text="CSV file path in simulation_campaign_datasets")
    overrides = models.JSONField(null=True, blank=True,
                                 help_text="Per-task overrides of ProdConfig fields (JSON)")

    # Generated commands (cached on save)
    condor_command = models.TextField(blank=True, default='',
                                      help_text="Generated Condor submission command")
    panda_command = models.TextField(blank=True, default='',
                                     help_text="Generated PanDA submission command")

    # Submission tracking
    panda_task_id = models.BigIntegerField(null=True, blank=True,
                                            help_text="PanDA task ID after submission")
    condor_cluster_id = models.CharField(max_length=100, blank=True, default='',
                                          help_text="Condor cluster ID after submission")

    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_prod_task'
        ordering = ['-updated_at']

    def __str__(self):
        return self.name

    def _dids_from_overrides(self, list_key, single_key=None):
        """Resolve a list of DIDs from overrides JSON.

        Reads ``overrides[list_key]`` if it is a non-empty list; else, if
        ``single_key`` is provided and present, wraps that single DID; else
        returns an empty list. JSON-only, no schema column — per
        PCS_DATASET_REQUEST_WORKFLOW.md interim model.
        """
        ov = self.overrides or {}
        dids = ov.get(list_key)
        if isinstance(dids, list) and dids:
            return [d for d in dids if d]
        if single_key:
            single = ov.get(single_key)
            if single:
                return [single]
        return []

    def _resolve_datasets(self, dids):
        """Resolve a list of DIDs to Dataset objects, preserving order.
        DIDs not found are silently dropped.
        """
        if not dids:
            return []
        by_did = {d.did: d for d in Dataset.objects.filter(did__in=dids)}
        return [by_did[d] for d in dids if d in by_did]

    @property
    def input_datasets(self):
        """List of input Datasets from overrides['input_dataset_dids'] or
        the singular 'input_dataset_did' (back-compat shortcut for the
        single-input EVGEN case)."""
        return self._resolve_datasets(
            self._dids_from_overrides('input_dataset_dids', 'input_dataset_did')
        )

    @property
    def output_datasets(self):
        """List of output Datasets from overrides['output_dataset_dids'].
        Falls back to ``[self.dataset]`` — the legacy single-output FK —
        when the override is unset."""
        dids = self._dids_from_overrides('output_dataset_dids')
        if dids:
            return self._resolve_datasets(dids)
        return [self.dataset] if self.dataset_id else []

    @property
    def intermediate_datasets(self):
        """List of intermediate Datasets from overrides['intermediate_dataset_dids']."""
        return self._resolve_datasets(
            self._dids_from_overrides('intermediate_dataset_dids')
        )

    @property
    def input_dataset(self):
        """Single helper: first of ``input_datasets`` (or None)."""
        inputs = self.input_datasets
        return inputs[0] if inputs else None

    @property
    def output_dataset(self):
        """Single helper: first of ``output_datasets`` — equivalent to the
        legacy ``self.dataset`` FK when no list override is set."""
        outputs = self.output_datasets
        return outputs[0] if outputs else None

    @property
    def outputs(self):
        """Produced Rucio datasets — one entry per dataset, never aggregated,
        lifecycle-neutral. The single home for the produced-output ↔ task
        association, stored in ``overrides['outputs']``; each entry is
        ``{did, stage, version, filters, rses:[{rse,files,total,complete}],
        file_count, bytes, complete, checked_at}``. Supersedes the old
        ``csv_import.output`` rollup and the ``past_output`` block — see
        EPICPROD_DATA_LINEAGE.md."""
        out = (self.overrides or {}).get('outputs')
        return out if isinstance(out, list) else []

    @property
    def has_output(self):
        """True if any produced Rucio dataset is recorded."""
        return bool(self.outputs)

    @property
    def output_stages(self):
        """Distinct production stages present in outputs, e.g. ['FULL', 'RECO']."""
        return sorted({o.get('stage') for o in self.outputs if o.get('stage')})

    @property
    def output_incomplete(self):
        """True if any produced dataset is not fully replicated at every RSE."""
        return any(not o.get('complete', True) for o in self.outputs)

    @property
    def input_source_kind(self):
        """Source kind of the external input. Linked Dataset wins; csv_file fallback."""
        ds = self.input_dataset
        if ds:
            return ds.source_kind
        return 'csv_manifest' if self.csv_file else ''

    @property
    def input_source_location(self):
        """Source location of the external input. Linked Dataset wins; csv_file fallback."""
        ds = self.input_dataset
        if ds:
            return ds.source_location
        return self.csv_file or ''

    @property
    def input_source_stage(self):
        """Stage of the external input. Linked Dataset wins; csv_file → 'evgen'."""
        ds = self.input_dataset
        if ds:
            return ds.stage
        return 'evgen' if self.csv_file else ''

    def get_effective_config(self):
        """Return ProdConfig field values with per-task overrides applied."""
        config = self.prod_config
        overrides = self.overrides or {}
        result = {}
        for field in config._meta.get_fields():
            if not hasattr(field, 'attname'):
                continue
            name = field.name
            if name in ('id', 'created_at', 'updated_at'):
                continue
            result[name] = overrides.get(name, getattr(config, name))
        # Merge the data dicts specially (override keys, not replace entire dict)
        base_data = config.data or {}
        override_data = overrides.get('data', {})
        if isinstance(override_data, dict):
            result['data'] = {**base_data, **override_data}
        return result

    def generate_commands(self):
        """Generate and cache both Condor and PanDA commands."""
        from .commands import build_condor_command, build_panda_command
        self.condor_command = build_condor_command(self)
        self.panda_command = build_panda_command(self)


def _allocate_simple_tag(state_key):
    """Atomically allocate the next tag number using PersistentState."""
    from monitor_app.models import PersistentState
    with transaction.atomic():
        obj, _ = PersistentState.objects.select_for_update().get_or_create(
            id=1, defaults={'state_data': {}}
        )
        current = obj.state_data.get(state_key, 1)
        obj.state_data[state_key] = current + 1
        obj.save()
        return current
