"""
EMI (ePIC Metadata Interface) data models.

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
        db_table = 'emi_physics_category'
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
        db_table = 'emi_physics_tag'
        ordering = ['tag_number']

    def __str__(self):
        return self.tag_label

    def save(self, *args, **kwargs):
        self.tag_label = f"p{self.tag_number}"
        super().save(*args, **kwargs)

    @classmethod
    def allocate_next(cls, category):
        """Atomically allocate the next tag number for the given category."""
        with transaction.atomic():
            base = category.digit * 1000
            last = (cls.objects.select_for_update()
                    .filter(category=category)
                    .order_by('-tag_number')
                    .values_list('tag_number', flat=True)
                    .first())
            return (last + 1) if last else base + 1


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
        db_table = 'emi_evgen_tag'
        ordering = ['tag_number']

    def __str__(self):
        return self.tag_label

    def save(self, *args, **kwargs):
        self.tag_label = f"e{self.tag_number}"
        super().save(*args, **kwargs)

    @classmethod
    def allocate_next(cls):
        return _allocate_simple_tag('emi_next_evgen')


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
        db_table = 'emi_simu_tag'
        ordering = ['tag_number']

    def __str__(self):
        return self.tag_label

    def save(self, *args, **kwargs):
        self.tag_label = f"s{self.tag_number}"
        super().save(*args, **kwargs)

    @classmethod
    def allocate_next(cls):
        return _allocate_simple_tag('emi_next_simu')


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
        db_table = 'emi_reco_tag'
        ordering = ['tag_number']

    def __str__(self):
        return self.tag_label

    def save(self, *args, **kwargs):
        self.tag_label = f"r{self.tag_number}"
        super().save(*args, **kwargs)

    @classmethod
    def allocate_next(cls):
        return _allocate_simple_tag('emi_next_reco')


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
    block_num = models.PositiveIntegerField(default=1)
    blocks = models.PositiveIntegerField(default=1)
    did = models.CharField(max_length=300, unique=True)
    file_count = models.IntegerField(default=0)
    data_size = models.BigIntegerField(default=0)
    description = models.TextField(blank=True, default='')
    metadata = models.JSONField(null=True, blank=True)
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'emi_dataset'
        ordering = ['-created_at']
        unique_together = [('dataset_name', 'block_num')]

    def __str__(self):
        return self.did

    def clean(self):
        for tag_field in ['physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag']:
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
        return (
            f"{self.scope}.{self.detector_version}.{self.detector_config}"
            f".{self.physics_tag.tag_label}.{self.evgen_tag.tag_label}"
            f".{self.simu_tag.tag_label}.{self.reco_tag.tag_label}"
        )


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

    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'emi_prod_config'
        ordering = ['-updated_at']

    def __str__(self):
        return self.name


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
