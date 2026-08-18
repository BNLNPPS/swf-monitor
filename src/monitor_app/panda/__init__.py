"""
PanDA query functions for ePIC production monitoring.

Pure synchronous functions that query the PanDA database (doma_panda schema).
Used by MCP tools and Django views alike.
"""

from .queries import (
    list_jobs,
    diagnose_jobs,
    job_completion_details,
    list_tasks,
    error_summary,
    get_activity,
    study_job,
    resource_usage,
    job_outcomes,
    list_queues,
    get_queue,
    queue_last_use,
    list_jobs_dt,
    build_tasks_window,
    job_filter_counts,
    task_filter_counts,
    get_task,
)

__all__ = [
    'list_jobs',
    'diagnose_jobs',
    'job_completion_details',
    'list_tasks',
    'error_summary',
    'get_activity',
    'study_job',
    'resource_usage',
    'job_outcomes',
    'list_queues',
    'get_queue',
    'queue_last_use',
    'list_jobs_dt',
    'build_tasks_window',
    'job_filter_counts',
    'task_filter_counts',
    'get_task',
]
