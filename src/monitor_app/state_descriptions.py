"""Short human descriptions for state values shown in the monitor.


Used by ``cell_fmt.fill_cell`` and the ``state_class`` filter variants to
attach a ``title=`` attribute to state-colored cells, so hovering a
"finished" or "failed" cell reveals what the state actually means.

Keys are lowercase; lookups go via ``state_description(value)``.
Unknown states return '' (no tooltip) — better silent than wrong.
"""

# Coverage: PanDA job/task states, workflow execution states, STF/run
# lifecycle states, agent statuses, log levels. Additions welcome;
# missing entries just omit the tooltip (no error).
STATE_DESCRIPTIONS = {
    # Log levels
    'debug': 'Debug-level log — verbose, developer-facing',
    'info': 'Informational log — normal operation',
    'warning': 'Warning — possible issue, operation continues',
    'error': 'Error — operation failed but service continues',
    'critical': 'Critical — service-level failure',

    # PanDA task / job states
    'defined': 'Defined but not yet queued for execution',
    'assigned': 'Assigned to a computing resource',
    'waiting': 'Waiting on upstream dependency',
    'pending': 'Queued, not yet started',
    'activated': 'Scheduled to run, awaiting pilot',
    'starting': 'Pilot starting the job',
    'sent': 'Dispatched to the pilot',
    'running': 'Actively executing',
    'transferring': 'Output transfer in progress',
    'holding': 'Awaiting follow-up action',
    'merging': 'Merging outputs',
    'finished': 'Completed successfully',
    'failed': 'Completed with failure',
    'cancelled': 'Cancelled by user or system',
    'closed': 'Closed — no further processing',
    'broken': 'Broken task — cannot recover automatically',
    'done': 'Task complete, all jobs finished',
    'aborted': 'Aborted before completion',
    'exhausted': 'All retry attempts exhausted',
    'obsolete': 'Superseded by a newer task',
    'ready': 'Ready to process',
    'scouting': 'Scout jobs running to probe resource',
    'scouted': 'Scout jobs complete, production can proceed',
    'prepared': 'Inputs prepared, awaiting job generation',
    'registered': 'Registered in the system',
    'submitting': 'Job submission in progress',
    'throttled': 'Rate-limited by the broker',
    'paused': 'Paused — resumable',

    # Workflow / agent states
    'ok': 'Healthy, sending heartbeats',
    'unknown': 'State unknown — no recent heartbeat',
    'exited': 'Process exited',
    'stopped': 'Stopped cleanly',
    'processing': 'Processing in progress',
    'processed': 'Processing complete',
    'active': 'Active / in progress',
    'completed': 'Completed',
    'idle': 'Idle, waiting for work',
    'skipped': 'Skipped by policy or upstream decision',
}


def state_description(value):
    """Return the short description for a state, or '' if unknown."""
    if not value:
        return ''
    return STATE_DESCRIPTIONS.get(str(value).lower(), '')
