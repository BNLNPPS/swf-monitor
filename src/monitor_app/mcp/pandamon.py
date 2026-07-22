"""
PanDA Monitor MCP tools — thin wrappers over panda.queries.

Each tool registers with the MCP server, provides an LLM-oriented docstring,
and delegates to the synchronous query function via sync_to_async.
"""

from asgiref.sync import sync_to_async
from ai.assessments import ai_content_retrieval_guidance
from monitor_app.epicprod_inventory import diagnosis_for_study_data
from monitor_app.mcp import mcp
from monitor_app.panda import queries


def _ai_content_for_panda_task(jeditaskid):
    if not jeditaskid:
        return ai_content_retrieval_guidance({})
    from pcs.models import PandaTasks
    row = PandaTasks.objects.filter(jedi_task_id=jeditaskid).first()
    return ai_content_retrieval_guidance(row.metadata if row else {})


def _list_tasks_sync(**kwargs):
    result = queries.list_tasks(**kwargs)
    if result.get('error'):
        return result
    taskid = kwargs.get('taskid')
    if taskid:
        for task in result.get('tasks') or []:
            task['ai_content'] = _ai_content_for_panda_task(task.get('jeditaskid'))
    return result


def _get_queue_sync(panda_queue):
    result = queries.get_queue(panda_queue=panda_queue)
    if result.get('error'):
        return result
    from monitor_app.models import PandaQueue
    row = PandaQueue.objects.filter(queue_name=panda_queue).first()
    result['ai_content'] = ai_content_retrieval_guidance(row.metadata if row else {})
    return result


def _study_job_sync(pandaid):
    result = queries.study_job(pandaid=pandaid)
    if result.get('error'):
        return result
    from monitor_app.models import EpicProdJob
    row = EpicProdJob.objects.filter(pandaid=pandaid).first()
    result['epicprod_diagnosis'] = diagnosis_for_study_data(result, epicprod_job=row)
    result['ai_content'] = ai_content_retrieval_guidance(row.data if row else {})
    return result


@mcp.tool()
async def panda_list_jobs(
    days: int = 7,
    status: str = None,
    username: str = None,
    site: str = None,
    taskid: int = None,
    reqid: int = None,
    limit: int = None,
    before_id: int = None,
) -> dict:
    """
    List PanDA jobs from the ePIC production database with summary statistics.

    Returns jobs in reverse time order (newest first) with cursor-based pagination.
    Use before_id to page through results: pass the last pandaid from the previous
    call to get the next batch.

    For a quick overview without individual records, use panda_get_activity instead.
    For error diagnostics on failed jobs, use panda_diagnose_jobs instead.

    Args:
        days: Time window in days (default 7). Jobs with modificationtime within this window.
        status: Filter by jobstatus (e.g. 'failed', 'finished', 'running', 'activated').
        username: Filter by job owner (produsername). Supports SQL LIKE with %.
        site: Filter by computing site (computingsite). Supports SQL LIKE with %.
        taskid: Filter by JEDI task ID (jeditaskid).
        reqid: Filter by request ID.
        limit: Maximum jobs to return. An explicit value is always honored.
            Default: 200, or the complete task population when taskid is given.
        before_id: Pagination cursor — return jobs with pandaid < this value.

    Returns:
        summary: Job counts by status for the full query (not just this page).
        jobs: List of job records with key fields. Each job includes
            destinationse — the destination storage element (Rucio SE) where
            output files are written, looked up from the files table.
        pagination: {before_id, has_more, next_before_id} for incremental pulling.
        total_in_window: Total jobs matching filters in the time window.
    """
    return await sync_to_async(queries.list_jobs)(
        days=days, status=status, username=username, site=site,
        taskid=taskid, reqid=reqid, limit=limit, before_id=before_id,
    )


@mcp.tool()
async def panda_diagnose_jobs(
    days: int = 7,
    username: str = None,
    site: str = None,
    taskid: int = None,
    reqid: int = None,
    error_component: str = None,
    limit: int = None,
    before_id: int = None,
) -> dict:
    """
    Diagnose failed and faulty PanDA jobs with full error details.

    Pulls only jobs in failed/cancelled/closed status with non-zero error codes.
    Returns all 7 error component fields (pilot, executor, DDM, brokerage,
    dispatcher, supervisor, taskbuffer) plus transformation exit code, distilled
    into a structured errors list per job.

    Use this after panda_list_jobs shows failures you want to understand.

    Args:
        days: Time window in days (default 7).
        username: Filter by job owner (produsername). Supports SQL LIKE with %.
        site: Filter by computing site. Supports SQL LIKE with %.
        taskid: Filter by JEDI task ID.
        reqid: Filter by request ID.
        error_component: Filter to jobs with errors in this component
                         (pilot, executor, ddm, brokerage, dispatcher, supervisor, taskbuffer).
        limit: Maximum jobs to return. An explicit value is always honored.
            Default: 500, or the complete task population when taskid is given.
        before_id: Pagination cursor — return jobs with pandaid < this value.

    Returns:
        error_summary: Counts by error component and top error codes.
        jobs: Failed jobs with full error details and structured errors list.
        pagination: {before_id, has_more, next_before_id} for incremental pulling.
    """
    return await sync_to_async(queries.diagnose_jobs)(
        days=days, username=username, site=site, taskid=taskid,
        reqid=reqid, error_component=error_component,
        limit=limit, before_id=before_id,
    )


@mcp.tool()
async def panda_list_tasks(
    days: int = 7,
    status: str = None,
    username: str = None,
    taskname: str = None,
    reqid: int = None,
    workinggroup: str = None,
    taskid: int = None,
    processingtype: str = None,
    limit: int = 500,
    before_id: int = None,
) -> dict:
    """
    List JEDI tasks from the ePIC production database with summary statistics.

    Tasks are higher-level units than jobs — each task spawns one or more jobs.
    Returns tasks in reverse ID order (newest first) with cursor-based pagination.

    Args:
        days: Time window in days (default 7). Tasks with modificationtime within this window.
        status: Filter by task status (e.g. 'done', 'failed', 'running', 'ready', 'broken', 'aborted').
        username: Filter by task owner. Supports SQL LIKE with %.
        taskname: Filter by task name — substring match (a fragment such as
            a campaign name matches); % wildcards are honored as given.
        reqid: Filter by request ID.
        workinggroup: Filter by working group (e.g. 'EIC', 'Rubin'). NULL for iDDS automation tasks.
        taskid: Filter by specific JEDI task ID (jeditaskid).
        processingtype: Filter by processing type (e.g. 'epicproduction'). Supports SQL LIKE with %.
        limit: Maximum tasks to return (default 500).
        before_id: Pagination cursor — return tasks with jeditaskid < this value.

    Returns:
        summary: Task counts by status for the full query (not just this page).
        tasks: List of task records. Each task includes native JEDI fields
            (status, failurerate, progress, taskname, username, workinggroup,
            processingtype, errordialog, creationdate, modificationtime, ...)
            PLUS aggregated per-task job counts:
              - nactive: jobs in non-terminal states (running, activated,
                starting, holding, transferring, merging, ...)
              - nfinished: jobs with jobstatus='finished'
              - nfailed: jobs with jobstatus='failed'
            Cancelled and closed jobs are deliberately NOT counted —
            operator-facing summaries should surface what operators don't
            already know (operators know when they cancel).
            failurerate is pre-computed at the file level; the
            nfailed/nfinished/nactive triple is job-level. Prefer
            failurerate when present; the job counts are useful for naming
            specific failing tasks (failures tend to correlate to a task
            by software or by running site).
        pagination: {before_id, has_more, next_before_id} for incremental pulling.
        total_in_window: Total tasks matching filters in the time window.
        When taskid is provided, matching task records also include
        `ai_content`. If `ai_content.available` is true, retrieve assessment
        rows by calling `ai_content.retrieval.tool` with
        `ai_content.retrieval.arguments`.
    """
    return await sync_to_async(_list_tasks_sync)(
        days=days, status=status, username=username, taskname=taskname,
        reqid=reqid, workinggroup=workinggroup, taskid=taskid,
        processingtype=processingtype, limit=limit, before_id=before_id,
    )


@mcp.tool()
async def panda_error_summary(
    days: int = 10,
    username: str = None,
    site: str = None,
    destinationse: str = None,
    taskid: int = None,
    error_source: str = None,
    limit: int = 20,
) -> dict:
    """
    Aggregate error summary across failed PanDA jobs, ranked by frequency.

    Extracts non-zero errors from all 7 error components (pilot, executor, DDM,
    brokerage, dispatcher, supervisor, taskbuffer) across failed/cancelled/closed
    jobs, groups by (component, code, diagnostic), and ranks by occurrence count.

    Unlike panda_diagnose_jobs (per-job detail), this tool gives the big picture:
    "What are the most common errors and who do they affect?"

    Args:
        days: Time window in days (default 10).
        username: Filter by job owner (produsername). Supports SQL LIKE with %.
        site: Filter by computing site (computingsite). Supports SQL LIKE with %.
        destinationse: Filter by destination storage element — the Rucio SE where
            output files are written, located at a site. Supports SQL LIKE with %.
        taskid: Filter by JEDI task ID.
        error_source: Filter to errors from one component
                      (pilot, executor, ddm, brokerage, dispatcher, supervisor, taskbuffer).
        limit: Maximum error patterns to return (default 20).

    Returns:
        total_errors: Total error occurrences across all components.
        errors: Ranked list of error patterns, each with:
            error_source, error_code, error_diag, count,
            task_count, users, sites, destination_sites.
    """
    return await sync_to_async(queries.error_summary)(
        days=days, username=username, site=site,
        destinationse=destinationse,
        taskid=taskid, error_source=error_source, limit=limit,
    )


@mcp.tool()
async def panda_get_activity(
    days: int = 1,
    username: str = None,
    site: str = None,
    workinggroup: str = None,
) -> dict:
    """
    Pre-digested overview of PanDA activity. No individual job/task records.

    Use this first to answer "What is PanDA doing?" before drilling into
    panda_list_jobs or panda_list_tasks for individual records.

    Args:
        days: Time window in days (default 1).
        username: Filter by job owner (produsername). Supports SQL LIKE with %.
        site: Filter by computing site (computingsite). Supports SQL LIKE with %.
        workinggroup: Filter tasks by working group (e.g. 'EIC').

    Returns:
        jobs: {total, by_status, by_user, by_site} — aggregate counts only.
        tasks: {total, by_status, by_user} — aggregate counts only.
        filters: Applied filter values.
    """
    return await sync_to_async(queries.get_activity)(
        days=days, username=username, site=site, workinggroup=workinggroup,
    )


@mcp.tool()
async def panda_list_queues(
    vo: str = None,
    status: str = None,
    state: str = None,
    search: str = None,
) -> dict:
    """
    List PanDA compute queues with configuration summary.

    Shows available queues from the PanDA schedconfig registry. Each queue
    represents a compute endpoint where jobs can be submitted.

    Args:
        vo: Filter by Virtual Organisation (e.g. 'eic', 'atlas', 'osg', 'lsst').
            Use 'eic' for ePIC experiment queues.
        status: Filter by queue status (e.g. 'online', 'brokeroff', 'offline').
        state: Filter by queue state (e.g. 'ACTIVE').
        search: Search queue name (case-insensitive, supports partial match).
                Example: 'Perlmutter' to find all NERSC Perlmutter queues.

    Returns:
        queues: List of queue summaries with status, VO, resource type, region, etc.
        count: Number of queues matching filters.
    """
    return await sync_to_async(queries.list_queues)(
        vo=vo, status=status, state=state, search=search,
    )


@mcp.tool()
async def panda_get_queue(
    panda_queue: str,
) -> dict:
    """
    Get full configuration for a single PanDA queue.

    Returns the complete schedconfig for a queue including container options,
    copy tools, storage endpoints, CE endpoints, resource limits, and all
    operational parameters.

    Args:
        panda_queue: The queue name (e.g. 'NERSC_Perlmutter_epic').

    Returns:
        queue: Full configuration dict with all parameters.
        ai_content: Availability flag, ids, and exact retrieval tool/arguments
            for append-only AI assessments linked to the local queue/site record.
    """
    return await sync_to_async(_get_queue_sync)(panda_queue=panda_queue)


@mcp.tool()
async def panda_resource_usage(
    days: int = 30,
    site: str = None,
    username: str = None,
    taskid: int = None,
    start_time: str = None,
    end_time: str = None,
    bucket: str = None,
) -> dict:
    """
    Aggregate resource usage (core-hours) for finished PanDA jobs.

    Reports two core-hour metrics:
    - allocated_core_hours: cores allocated × wall time
    - used_core_hours: CPU time actually consumed by the job

    The gap between allocated and used reflects efficiency — e.g. a job that
    requests 1 core but gets 2 allocated uses ~50% of its allocation.

    Only counts finished jobs with actual runtime (starttime and endtime set).
    Jobs are attributed by endtime; queue/waiting time is excluded.

    Args:
        days: Time window in days ending at end_time (default 30). Used when
              start_time is not supplied.
        site: Filter by computing site (computingsite). Supports SQL LIKE with %.
              Example: 'NERSC_Perlmutter%' for all Perlmutter queues.
        username: Filter by job owner (produsername). Supports SQL LIKE with %.
        taskid: Filter by JEDI task ID.
        start_time: Optional ISO-8601 start timestamp, inclusive.
        end_time: Optional ISO-8601 end timestamp, exclusive. Defaults to now.
        bucket: Optional time-series bin: 'day' or 'week'.

    Returns:
        totals: {job_count, allocated_core_hours, used_core_hours, wall_hours}
        by_site: Breakdown by computing site, sorted by allocated_core_hours.
        by_user: Breakdown by job owner, sorted by allocated_core_hours.
        series: Site breakdown per day/week when bucket is supplied.
        window: Exact start, end, bucket, and time field used.
    """
    return await sync_to_async(queries.resource_usage)(
        days=days, site=site, username=username, taskid=taskid,
        start_time=start_time, end_time=end_time, bucket=bucket,
    )


@mcp.tool()
async def panda_study_job(
    pandaid: int,
) -> dict:
    """
    Deep study of a single PanDA job — full record, files, errors, log URLs,
    and ePIC production diagnosis.

    Gathers everything available from the database for a single job:
    - Full job record with all error fields and resource usage
    - Associated files from filestable4 (input, output, log)
    - Harvester worker info with condor log URLs
    - Parent task context (name, status, error dialog)
    - Structured error extraction across all 7 components
    - ePIC production diagnosis from app inventory, including parsed phase,
      failure summary, and payload-log timeline when available

    Use this after panda_diagnose_jobs identifies a failed job you want to
    understand in detail. Check `epicprod_diagnosis.available` first; when true,
    prefer `epicprod_diagnosis.phase` and `failure_summary` for the
    production-facing failure explanation before falling back to PanDA pilot
    diagnostics. This matters for ePIC payload-managed data workflows where
    JLab Rucio input/output failures are visible in payload logs rather than
    PanDA-managed file tables. Returns log URLs for manual inspection even when
    programmatic log retrieval is not yet available.

    Args:
        pandaid: The PanDA job ID to study (required).

    Returns:
        job: Full job record (null fields stripped) with structured errors list.
        files: All associated files (log, output, input) with lfn, guid, scope, status.
        log_urls: URLs for pilot stdout, stderr, batch log (require CILogon auth).
        log_file: Log tarball metadata if registered (lfn, guid, scope for rucio retrieval).
        harvester: Condor worker details if available.
        task: Parent JEDI task context.
        monitor_url: Link to PanDA monitoring page.
        epicprod_diagnosis: {available, phase, failure_summary, timeline,
            last_refreshed_at, guidance}. Use this for the production-facing
            phase of failed ePIC jobs.
        ai_content: Availability flag, ids, and exact retrieval tool/arguments
            for append-only AI assessments linked to the local job record.
    """
    return await sync_to_async(_study_job_sync)(pandaid=pandaid)


@mcp.tool()
async def panda_harvester_workers(
    site: str | None = None,
    hours: int = 1,
) -> dict:
    """
    Live Harvester pilot/worker counts across EIC compute queues.

    Shows how many pilots are running, submitted, finished, etc. at each site.
    Useful for checking if Perlmutter or other sites are actively processing.

    Args:
        site: Filter to a specific queue (e.g. 'NERSC_Perlmutter_epic'). Default: all sites.
        hours: Time window in hours to look back (default 1).

    Returns:
        nworkers_total: Grand total across all statuses.
        nworkers_by_status: Counts by worker status (running, submitted, finished, etc.).
        nworkers_by_site: Counts by computing site.
        pivot: Breakdown by status × jobtype × resourcetype.
    """
    from datetime import datetime, timedelta, timezone
    from askpanda_atlas.harvester_worker_impl import fetch_worker_stats
    from decouple import config

    base_url = config('PANDA_BASE_URL', default='https://pandamon01.sdcc.bnl.gov')
    now = datetime.now(timezone.utc)
    from_dt = (now - timedelta(hours=hours)).isoformat()
    to_dt = now.isoformat()
    raw = await sync_to_async(fetch_worker_stats)(
        base_url, from_dt, to_dt, site=site,
    )
    if raw.get('error'):
        return {"error": raw['error']}
    return {
        "summary": (
            f"{raw.get('nworkers_total', 0)} pilots total"
            + (f" at {site}" if site else " across all EIC sites")
            + f" (last {hours}h)"
        ),
        "by_status": raw.get('nworkers_by_status', {}),
        "by_site": raw.get('nworkers_by_site', {}),
        "by_resourcetype": raw.get('nworkers_by_resourcetype', {}),
        "time_window": {"from": from_dt, "to": to_dt},
    }
