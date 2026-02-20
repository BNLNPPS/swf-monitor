"""
PanDA Monitor MCP tools — thin wrappers over panda.queries.

Each tool registers with the MCP server, provides an LLM-oriented docstring,
and delegates to the synchronous query function via sync_to_async.
"""

from asgiref.sync import sync_to_async
from mcp_server import mcp_server as mcp
from panda import queries


@mcp.tool()
async def panda_list_jobs(
    days: int = 7,
    status: str = None,
    username: str = None,
    site: str = None,
    taskid: int = None,
    reqid: int = None,
    limit: int = 200,
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
        limit: Maximum jobs to return (default 200).
        before_id: Pagination cursor — return jobs with pandaid < this value.

    Returns:
        summary: Job counts by status for the full query (not just this page).
        jobs: List of job records with key fields.
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
    limit: int = 500,
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
        limit: Maximum jobs to return (default 500).
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
    limit: int = 25,
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
        taskname: Filter by task name. Supports SQL LIKE with %.
        reqid: Filter by request ID.
        workinggroup: Filter by working group (e.g. 'EIC', 'Rubin'). NULL for iDDS automation tasks.
        taskid: Filter by specific JEDI task ID (jeditaskid).
        limit: Maximum tasks to return (default 25).
        before_id: Pagination cursor — return tasks with jeditaskid < this value.

    Returns:
        summary: Task counts by status for the full query (not just this page).
        tasks: List of task records with key fields.
        pagination: {before_id, has_more, next_before_id} for incremental pulling.
        total_in_window: Total tasks matching filters in the time window.
    """
    return await sync_to_async(queries.list_tasks)(
        days=days, status=status, username=username, taskname=taskname,
        reqid=reqid, workinggroup=workinggroup, taskid=taskid,
        limit=limit, before_id=before_id,
    )


@mcp.tool()
async def panda_error_summary(
    days: int = 10,
    username: str = None,
    site: str = None,
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
        site: Filter by computing site. Supports SQL LIKE with %.
        taskid: Filter by JEDI task ID.
        error_source: Filter to errors from one component
                      (pilot, executor, ddm, brokerage, dispatcher, supervisor, taskbuffer).
        limit: Maximum error patterns to return (default 20).

    Returns:
        total_errors: Total error occurrences across all components.
        errors: Ranked list of error patterns, each with:
            error_source, error_code, error_diag, count,
            task_count, users, sites.
    """
    return await sync_to_async(queries.error_summary)(
        days=days, username=username, site=site,
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
