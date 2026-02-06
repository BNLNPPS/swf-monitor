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
        ('EXITED', 'Exited'),
    ]

    OPERATIONAL_STATE_CHOICES = [
        ('STARTING', 'Starting'),
        ('READY', 'Ready'),
        ('PROCESSING', 'Processing'),
        ('EXITED', 'Exited'),
    ]

    AGENT_TYPE_CHOICES = [
        ('daqsim', 'DAQ Simulator'),
        ('data', 'Data Agent'),
        ('processing', 'Processing Agent'),
        ('fastmon', 'Fast Monitoring Agent'),
        ('workflow_runner', 'Workflow Runner'),
        ('monitor', 'Monitor System'),
        ('sse_sender', 'SSE Test Sender'),
        ('sse_receiver', 'SSE Client/Receiver'),
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

    # Process identification for agent management
    pid = models.IntegerField(null=True, blank=True,
                              help_text="Process ID for kill operations")
    hostname = models.CharField(max_length=100, null=True, blank=True,
                                help_text="Host where agent is running")
    operational_state = models.CharField(
        max_length=20,
        choices=OPERATIONAL_STATE_CHOICES,
        default='STARTING',
        help_text="What the agent is doing (STARTING/READY/PROCESSING/EXITED)"
    )

    # Namespace - identifies the testbed instance this agent belongs to
    namespace = models.CharField(max_length=100, null=True, blank=True, db_index=True,
                                 help_text="Testbed namespace for workflow delineation")

    # Extensible metadata
    metadata = models.JSONField(null=True, blank=True,
                                help_text="Extensible metadata for agent configuration and state")

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
    Represents a message queue subscriber in the monitoring system. 
    Subscribers receive notifications about STF files via ActiveMQ directly or SSE.
    
    Attributes:
        subscriber_id: Auto-incrementing primary key
        subscriber_name: Unique name identifying the subscriber
        fraction: Fraction of messages to receive  
        description: Human-readable description of the subscriber
        is_active: Whether the subscriber is currently active
        created_at: Timestamp when record was created
        updated_at: Timestamp when record was last updated
        delivery_type: How messages are delivered (activemq or sse)
        client_ip: IP address for SSE subscribers
        client_location: Geographic location for SSE subscribers
        connected_at: When SSE subscriber connected
        disconnected_at: When SSE subscriber disconnected
        last_activity: Last activity timestamp for SSE subscribers
        message_filters: JSON filters for SSE message selection
        messages_received: Count of messages received
        messages_sent: Count of messages sent (SSE)
        messages_dropped: Count of messages dropped due to queue overflow (SSE)
    """
    DELIVERY_TYPE_CHOICES = [
        ('activemq', 'ActiveMQ Direct'),
        ('sse', 'Server-Sent Events'),
    ]
    
    subscriber_id = models.AutoField(primary_key=True)
    subscriber_name = models.CharField(max_length=255, unique=True)
    fraction = models.FloatField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # New fields for SSE support
    delivery_type = models.CharField(
        max_length=20, 
        choices=DELIVERY_TYPE_CHOICES, 
        default='activemq'
    )
    
    # SSE-specific connection info (null for ActiveMQ subscribers)
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    client_location = models.CharField(max_length=255, blank=True, default='')
    connected_at = models.DateTimeField(null=True, blank=True)
    disconnected_at = models.DateTimeField(null=True, blank=True)
    last_activity = models.DateTimeField(null=True, blank=True)
    
    # Message filters (for SSE subscribers)
    # Format: {"msg_types": ["stf_gen"], "agents": ["daq-simulator"], "run_ids": [1001]}
    message_filters = models.JSONField(default=dict, blank=True)
    
    # Statistics (applicable to both types)
    messages_received = models.IntegerField(default=0)
    messages_sent = models.IntegerField(default=0)  # For SSE
    messages_dropped = models.IntegerField(default=0)  # For SSE queue overflow

    class Meta:
        db_table = 'swf_subscribers'
        indexes = [
            models.Index(fields=['delivery_type', 'is_active']),
        ]

    def __str__(self):
        return self.subscriber_name



class FastMonFile(models.Model):
    """
    Represents a Time Frame (TF) file for fast monitoring.
    TF files are subsamples of Super Time Frame (STF) files, processed for rapid monitoring.

    Attributes:
        tf_file_id: UUID primary key for unique TF file identification
        stf_file: Foreign key to the parent STF file this TF is derived from
        tf_filename: Unique filename for the TF file
        file_size_bytes: Size of the TF file in bytes
        checksum: File integrity checksum
        status: Current processing status (FileStatus enum)
        metadata: JSON field for flexible storage of TF-specific metadata
        created_at: Timestamp when TF record was created
        updated_at: Timestamp when TF record was last modified
    """
    tf_file_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stf_file = models.ForeignKey(StfFile, on_delete=models.CASCADE, related_name='tf_files')
    tf_filename = models.CharField(max_length=255, unique=True)
    file_size_bytes = models.BigIntegerField(null=True, blank=True)
    checksum = models.CharField(max_length=64, null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=FileStatus.choices,
        default=FileStatus.REGISTERED
    )
    metadata = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'swf_fastmon_files'

    def __str__(self):
        return f"TF File {self.tf_filename}"


class TFSlice(models.Model):
    """
    Represents a Time Frame slice for fast processing workflow.
    Each TF slice is a small portion (~15 per STF sample) that can be
    processed independently by workers in ~30 seconds.
    """
    slice_id = models.IntegerField()  # Serial number within STF sample (1-15)
    tf_first = models.IntegerField()  # First TF in the range
    tf_last = models.IntegerField()   # Last TF in the range
    tf_count = models.IntegerField()  # Number of TFs in the slice
    tf_filename = models.CharField(max_length=255, db_index=True)
    stf_filename = models.CharField(max_length=255, db_index=True)
    run_number = models.IntegerField(db_index=True)
    status = models.CharField(max_length=20, default='queued')
    retries = models.IntegerField(default=0)
    metadata = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Processing tracking
    assigned_worker = models.CharField(max_length=255, null=True, blank=True)
    assigned_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'swf_tf_slices'
        indexes = [
            models.Index(fields=['run_number', 'status']),
            models.Index(fields=['stf_filename', 'status']),
            models.Index(fields=['status', 'created_at']),
        ]
        unique_together = [['tf_filename', 'slice_id']]

    def __str__(self):
        return f"Slice {self.slice_id} of {self.tf_filename}"


class Worker(models.Model):
    """
    Tracks workers processing TF slices in the fast processing workflow.
    Records both active and inactive workers for historical analysis.
    """
    worker_id = models.CharField(max_length=255, primary_key=True)
    run_number = models.IntegerField(db_index=True)
    panda_job = models.CharField(max_length=255)
    location = models.CharField(max_length=255)  # batch queue
    status = models.CharField(max_length=20)
    current_slice_id = models.IntegerField(null=True, blank=True)
    tf_filename = models.CharField(max_length=255, null=True, blank=True)
    slices_completed = models.IntegerField(default=0)
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = 'swf_workers'
        indexes = [
            models.Index(fields=['run_number', 'status']),
        ]

    def __str__(self):
        return f"Worker {self.worker_id}"


class RunState(models.Model):
    """
    Tracks the current processing state for each run in the fast processing workflow.
    Provides quick access to run-level statistics and status.
    """
    run_number = models.IntegerField(primary_key=True)
    phase = models.CharField(max_length=20)
    state = models.CharField(max_length=20)
    substate = models.CharField(max_length=20, null=True, blank=True)
    target_worker_count = models.IntegerField()
    active_worker_count = models.IntegerField(default=0)
    stf_samples_received = models.IntegerField(default=0)
    slices_created = models.IntegerField(default=0)
    slices_queued = models.IntegerField(default=0)
    slices_processing = models.IntegerField(default=0)
    slices_completed = models.IntegerField(default=0)
    slices_failed = models.IntegerField(default=0)
    state_changed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)
    metadata = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = 'swf_run_state'

    def __str__(self):
        return f"Run {self.run_number} - {self.state}/{self.phase}"


class SystemStateEvent(models.Model):
    """
    Event log for time-travel replay of system state.
    Records all significant events in the fast processing workflow.
    """
    event_id = models.AutoField(primary_key=True)
    timestamp = models.DateTimeField(db_index=True)
    run_number = models.IntegerField(db_index=True)
    event_type = models.CharField(max_length=50, db_index=True)
    state = models.CharField(max_length=20, db_index=True)
    substate = models.CharField(max_length=20, null=True, blank=True, db_index=True)
    event_data = models.JSONField()

    class Meta:
        db_table = 'swf_system_state_events'
        indexes = [
            models.Index(fields=['timestamp', 'run_number']),
        ]

    def __str__(self):
        return f"Event {self.event_id} - {self.event_type} at {self.timestamp}"


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

    @classmethod
    def get_next_agent_id(cls):
        """Get next agent ID atomically and update last agent info."""
        from django.db import transaction
        from django.utils import timezone

        with transaction.atomic():
            obj, created = cls.objects.select_for_update().get_or_create(
                id=1,
                defaults={'state_data': {
                    'next_agent_id': 1,  # Start at 1
                    'last_agent_id': None,
                    'last_agent_registration_time': None
                }}
            )

            # Initialize if missing
            if 'next_agent_id' not in obj.state_data:
                obj.state_data['next_agent_id'] = 1  # Start at 1

            current_agent_id = obj.state_data['next_agent_id']
            current_time = timezone.now().isoformat()

            # Update state for this agent
            obj.state_data.update({
                'next_agent_id': current_agent_id + 1,
                'last_agent_id': current_agent_id,
                'last_agent_registration_time': current_time
            })
            obj.save()

            return current_agent_id

    @classmethod
    def get_next_workflow_execution_id(cls):
        """Get next workflow execution sequence number atomically."""
        from django.db import transaction
        from django.utils import timezone

        with transaction.atomic():
            obj, created = cls.objects.select_for_update().get_or_create(
                id=1,
                defaults={'state_data': {
                    'next_workflow_execution_id': 1,
                    'last_workflow_execution_id': None,
                    'last_workflow_execution_time': None
                }}
            )

            current_time = timezone.now().isoformat()
            current_id = obj.state_data.get('next_workflow_execution_id', 1)

            obj.state_data.update({
                'next_workflow_execution_id': current_id + 1,
                'last_workflow_execution_id': current_id,
                'last_workflow_execution_time': current_time
            })
            obj.save()

            return current_id


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


class AIMemory(models.Model):
    """
    AI dialogue history for cross-session context.

    Records exchanges between developers and AI assistants (Claude Code)
    to enable context continuity across sessions. Opt-in via SWF_DIALOGUE_TURNS env var.
    """
    id = models.AutoField(primary_key=True)
    username = models.CharField(max_length=100, db_index=True,
                                help_text="Developer username")
    session_id = models.CharField(max_length=255, db_index=True,
                                  help_text="Claude Code session ID")
    role = models.CharField(max_length=20,
                           help_text="'user' or 'assistant'")
    content = models.TextField(help_text="Message content")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Optional context
    namespace = models.CharField(max_length=100, null=True, blank=True,
                                help_text="Testbed namespace if applicable")
    project_path = models.CharField(max_length=500, null=True, blank=True,
                                   help_text="Project directory path")

    class Meta:
        db_table = 'swf_ai_memory'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['username', '-created_at']),
        ]

    def __str__(self):
        preview = self.content[:50] + '...' if len(self.content) > 50 else self.content
        return f"{self.username}/{self.role}: {preview}"


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
