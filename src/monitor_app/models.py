import logging
import uuid
from django.db import models
from django.utils import timezone


class SystemAgent(models.Model):
    STATUS_CHOICES = [
        ('UNKNOWN', 'Unknown'),
        ('OK', 'OK'),
        ('WARNING', 'Warning'),
        ('ERROR', 'Error'),
    ]
    
    AGENT_TYPE_CHOICES = [
        ('daqsim', 'DAQ Simulator'),
        ('data', 'Data Agent'),
        ('processing', 'Processing Agent'),
        ('fastmon', 'Fast Monitoring Agent'),
        ('monitor', 'Monitor System'),
        ('test', 'Test Agent'),
        ('other', 'Other'),
    ]

    instance_name = models.CharField(max_length=100, unique=True)
    agent_type = models.CharField(max_length=20, choices=AGENT_TYPE_CHOICES)
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default='UNKNOWN',
    )
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    agent_url = models.URLField(max_length=200, blank=True, null=True)
    
    # Workflow-specific fields
    workflow_enabled = models.BooleanField(default=True)
    current_stf_count = models.IntegerField(default=0)
    total_stf_processed = models.IntegerField(default=0)
    last_stf_processed = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'swf_systemagent'

    def __str__(self):
        return self.instance_name
        
    def is_workflow_agent(self):
        """Check if this agent participates in STF workflow."""
        return self.agent_type in ['daqsim', 'data', 'processing', 'fastmon']
        
    def update_stf_stats(self, increment_current=0, increment_total=0):
        """Update STF processing statistics."""
        self.current_stf_count += increment_current
        self.total_stf_processed += increment_total
        if increment_total > 0:
            self.last_stf_processed = timezone.now()
        self.save()

class AppLog(models.Model):
    LEVEL_CHOICES = [
        (logging.CRITICAL, 'CRITICAL'),
        (logging.ERROR, 'ERROR'),
        (logging.WARNING, 'WARNING'),
        (logging.INFO, 'INFO'),
        (logging.DEBUG, 'DEBUG'),
        (logging.NOTSET, 'NOTSET'),
    ]
    app_name = models.CharField(max_length=100, db_index=True)
    instance_name = models.CharField(max_length=100, db_index=True)
    timestamp = models.DateTimeField(db_index=True)
    level = models.IntegerField(choices=LEVEL_CHOICES, default=logging.NOTSET, db_index=True)
    levelname = models.CharField(max_length=50)
    message = models.TextField()
    module = models.CharField(max_length=255)
    funcname = models.CharField(max_length=255)
    lineno = models.IntegerField()
    process = models.IntegerField()
    thread = models.BigIntegerField()
    extra_data = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = 'swf_applog'
        ordering = ['-timestamp']
        verbose_name_plural = "App Logs"
        indexes = [
            models.Index(fields=['timestamp', 'app_name', 'instance_name']),
        ]

    def __str__(self):
        return f'{self.timestamp} - {self.app_name}:{self.instance_name} - {self.get_level_display()} - {self.message}'

"""
Django models for the SWF Fast Monitoring Agent database.

This module defines the core data models for tracking Super Time Frame (STF) files, message queue subscribers, 
and dispatch operations in the ePIC streaming workflow testbed.
"""

class FileStatus(models.TextChoices):
    """
    Status choices for STF file processing lifecycle.
    Tracks the processing state of Super Time Frame files from initial registration through final message queue dispatch.
    
    Registered: file added to the DB 
    Processing: Any pre-treatment before dispatching to MQ
    Processed: Pre-treatment complete, ready to dispatch 
    Done: sent to MQ
    Failed: Some problem in the workflow
    """
    REGISTERED = "registered", "Registered"
    PROCESSING = "processing", "Processing"
    PROCESSED = "processed", "Processed"
    FAILED = "failed", "Failed"
    DONE = "done", "Done"


class Run(models.Model):
    """
    Represents a data-taking run in the ePIC detector system.
    
    Attributes:
        run_id: Auto-incrementing primary key
        run_number: Unique identifier for the run, defined by DAQ 
        start_time: When the run began
        end_time: When the run ended (null if still active)
        run_conditions: JSON field storing experimental conditions
    """
    run_id = models.AutoField(primary_key=True)
    run_number = models.IntegerField(unique=True)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    run_conditions = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = 'swf_runs'

    def __str__(self):
        return f"Run {self.run_number}"


class StfFile(models.Model):
    """
    Represents a Super Time Frame (STF) file in the data acquisition system.
    Each file is tracked with metadata, processing status, and location
    information for monitoring and message queue dispatch.
    
    Attributes:
        file_id: UUID primary key for unique file identification
        run: Foreign key to the associated Run
        machine_state: Detector state during data collection (e.g., "physics", "cosmics")
        file_url: URL location of the STF file, intended for remote access
        file_size_bytes: Size of the file in bytes
        checksum: File integrity checksum
        created_at: Timestamp when file record was created
        status: Current processing status (FileStatus enum)
        metadata: JSON field for additional file metadata
    """
    file_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name='stf_files')
    machine_state = models.CharField(max_length=64, default="physics")
    stf_filename = models.CharField(max_length=255, unique=True)
    file_size_bytes = models.BigIntegerField(null=True, blank=True)
    checksum = models.CharField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=20,
        choices=FileStatus.choices,
        default=FileStatus.REGISTERED
    )
    metadata = models.JSONField(null=True, blank=True)
    
    # Workflow integration fields
    workflow_id = models.UUIDField(null=True, blank=True, db_index=True)
    daq_state = models.CharField(max_length=20, null=True, blank=True)
    daq_substate = models.CharField(max_length=20, null=True, blank=True)
    workflow_status = models.CharField(max_length=30, null=True, blank=True)

    class Meta:
        db_table = 'swf_stf_files'

    def __str__(self):
        return f"STF File {self.file_id}"


class Subscriber(models.Model):
    """
    Represents a message queue subscriber in the monitoring system. Subscribers receive notifications about STF files.
    
    Attributes:
        subscriber_id: Auto-incrementing primary key
        subscriber_name: Unique name identifying the subscriber
        fraction: Fraction of messages to receive  
        description: Human-readable description of the subscriber
        is_active: Whether the subscriber is currently active
        created_at: Timestamp when record was created
        updated_at: Timestamp when record was last updated
    """
    subscriber_id = models.AutoField(primary_key=True)
    subscriber_name = models.CharField(max_length=255, unique=True)
    fraction = models.FloatField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'swf_subscribers'

    def __str__(self):
        return self.subscriber_name


class MessageQueueDispatch(models.Model):
    """
    Records message queue dispatch operations for STF file events.
    
    Tracks when and how STF file notifications are sent to message queues, including success/failure status and error 
    details for monitoring.
    
    Attributes:
        dispatch_id: UUID primary key for unique dispatch identification
        stf_file: Foreign key to the associated STF file
        dispatch_time: Timestamp when the dispatch occurred (auto_now_add)
        message_content: JSON content of the dispatched message
        is_successful: Whether the dispatch succeeded
        error_message: Error details if dispatch failed
    """
    dispatch_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stf_file = models.ForeignKey(StfFile, on_delete=models.CASCADE, related_name='dispatches')
    dispatch_time = models.DateTimeField(auto_now_add=True)
    message_content = models.JSONField(null=True, blank=True)
    is_successful = models.BooleanField(null=True, default=None)
    error_message = models.TextField(null=True, blank=True)
    
    # Workflow integration fields
    workflow_id = models.UUIDField(null=True, blank=True, db_index=True)
    message_type = models.CharField(max_length=50, null=True, blank=True)
    sender_agent = models.CharField(max_length=100, null=True, blank=True)
    recipient_agent = models.CharField(max_length=100, null=True, blank=True)

    class Meta:
        db_table = 'swf_message_queue_dispatches'

    def __str__(self):
        return f"Dispatch {self.dispatch_id} - STF {self.stf_file.file_id} - {'Success' if self.is_successful else 'Failed'}"


class FastMonFile(models.Model):
    """
    Represents a Time Frame (TF) file for fast monitoring.
    TF files are subsamples of Super Time Frame (STF) files, processed for rapid monitoring.
    
    Attributes:
        tf_file_id: UUID primary key for unique TF file identification
        stf_file: Foreign key to the parent STF file this TF is derived from
        tf_filename: Unique filename for the TF file
        sequence_number: Position of this TF within the STF subsample sequence
        file_size_bytes: Size of the TF file in bytes
        checksum: File integrity checksum
        status: Current processing status (FileStatus enum)
        metadata: JSON field for flexible storage of TF-specific metadata
        workflow_id: Optional UUID linking to STFWorkflow
        fastmon_agent: Name of the fast monitoring agent that processed this TF
        created_at: Timestamp when TF record was created
        updated_at: Timestamp when TF record was last modified
    """
    tf_file_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stf_file = models.ForeignKey(StfFile, on_delete=models.CASCADE, related_name='tf_files')
    tf_filename = models.CharField(max_length=255, unique=True)
    sequence_number = models.IntegerField()
    file_size_bytes = models.BigIntegerField(null=True, blank=True)
    checksum = models.CharField(max_length=64, null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=FileStatus.choices,
        default=FileStatus.REGISTERED
    )
    metadata = models.JSONField(null=True, blank=True)
    
    # Optional workflow integration fields
    workflow_id = models.UUIDField(null=True, blank=True, db_index=True)
    fastmon_agent = models.CharField(max_length=100, null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'swf_fastmon_files'
        ordering = ['stf_file', 'sequence_number']
        indexes = [
            models.Index(fields=['stf_file', 'sequence_number']),
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['workflow_id']),
        ]
        unique_together = [['stf_file', 'sequence_number']]

    def __str__(self):
        return f"TF File {self.tf_filename} (seq: {self.sequence_number})"


class PersistentState(models.Model):
    """
    Persistent state store with stable schema - just stores JSON.
    Never modify this schema - it must remain stable across all deployments.
    
    Single record stores all persistent state as JSON blob.
    Use get_state() and update_state() methods to access nested data.
    """
    id = models.AutoField(primary_key=True)  # Always have ID=1
    state_data = models.JSONField(default=dict)  # All state stored here
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'swf_persistent_state'
        
    @classmethod
    def get_state(cls):
        """Get the complete state JSON object."""
        obj, created = cls.objects.get_or_create(id=1, defaults={'state_data': {}})
        return obj.state_data
    
    @classmethod
    def update_state(cls, updates):
        """Update state with new values (dict merge)."""
        from django.db import transaction
        with transaction.atomic():
            obj, created = cls.objects.select_for_update().get_or_create(
                id=1, 
                defaults={'state_data': {}}
            )
            obj.state_data.update(updates)
            obj.save()
            return obj.state_data
    
    @classmethod
    def get_next_run_number(cls):
        """Get next run number atomically and update last run info."""
        from django.db import transaction
        from django.utils import timezone
        
        with transaction.atomic():
            obj, created = cls.objects.select_for_update().get_or_create(
                id=1,
                defaults={'state_data': {
                    'next_run_number': 100010,  # Start higher to avoid test data conflicts
                    'last_run_number': None,
                    'last_run_start_time': None
                }}
            )
            
            # Initialize if missing
            if 'next_run_number' not in obj.state_data:
                obj.state_data['next_run_number'] = 100010  # Start higher to avoid test data conflicts
            
            current_run = obj.state_data['next_run_number']
            current_time = timezone.now().isoformat()
            
            # Update state for this run
            obj.state_data.update({
                'next_run_number': current_run + 1,
                'last_run_number': current_run,
                'last_run_start_time': current_time
            })
            obj.save()
            
            return current_run


class PandaQueue(models.Model):
    """
    Represents a PanDA compute queue configuration.
    Stores the queue name and full configuration as JSON.
    """
    queue_name = models.CharField(max_length=100, unique=True, primary_key=True)
    site = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=50, default='active')
    queue_type = models.CharField(max_length=50, blank=True)
    config_data = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'swf_panda_queues'
        ordering = ['queue_name']
        verbose_name = 'PanDA Queue'
        verbose_name_plural = 'PanDA Queues'
    
    def __str__(self):
        return self.queue_name


class RucioEndpoint(models.Model):
    """
    Represents a Rucio DDM (Distributed Data Management) endpoint configuration.
    Stores the endpoint name and full configuration as JSON.
    """
    endpoint_name = models.CharField(max_length=100, unique=True, primary_key=True)
    site = models.CharField(max_length=100, blank=True)
    endpoint_type = models.CharField(max_length=50, blank=True)
    is_tape = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    config_data = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'swf_rucio_endpoints'
        ordering = ['endpoint_name']
        verbose_name = 'Rucio Endpoint'
        verbose_name_plural = 'Rucio Endpoints'
    
    def __str__(self):
        return self.endpoint_name


# Import workflow models to register them with Django
from .workflow_models import (
    STFWorkflow,
    AgentWorkflowStage,
    WorkflowMessage,
    DAQState,
    DAQSubstate,
    WorkflowStatus,
    AgentType,
)
