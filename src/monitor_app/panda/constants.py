"""
PanDA database schema constants for ePIC production monitoring.

Field lists, error component definitions, and schema identifiers
shared across query functions and SQL builders.
"""

PANDA_SCHEMA = 'doma_panda'

# Job field lists

LIST_FIELDS = [
    'pandaid', 'jeditaskid', 'reqid', 'produsername', 'jobstatus',
    'computingsite', 'transformation', 'processingtype',
    'creationtime', 'starttime', 'endtime', 'modificationtime',
    'corecount', 'nevents',
    'noutputdatafiles', 'outputfilebytes',
]

ERROR_FIELDS = [
    'brokerageerrorcode', 'brokerageerrordiag',
    'ddmerrorcode', 'ddmerrordiag',
    'exeerrorcode', 'exeerrordiag',
    'jobdispatchererrorcode', 'jobdispatchererrordiag',
    'piloterrorcode', 'piloterrordiag',
    'superrorcode', 'superrordiag',
    'taskbuffererrorcode', 'taskbuffererrordiag',
    'transexitcode',
]

DIAGNOSE_EXTRA_FIELDS = [
    'jobname', 'pilotid', 'computingelement', 'jobmetrics',
    'specialhandling', 'commandtopilot', 'maxrss', 'maxpss',
]

ERROR_COMPONENTS = [
    {'name': 'brokerage', 'code': 'brokerageerrorcode', 'diag': 'brokerageerrordiag'},
    {'name': 'ddm', 'code': 'ddmerrorcode', 'diag': 'ddmerrordiag'},
    {'name': 'executor', 'code': 'exeerrorcode', 'diag': 'exeerrordiag'},
    {'name': 'dispatcher', 'code': 'jobdispatchererrorcode', 'diag': 'jobdispatchererrordiag'},
    {'name': 'pilot', 'code': 'piloterrorcode', 'diag': 'piloterrordiag'},
    {'name': 'supervisor', 'code': 'superrorcode', 'diag': 'superrordiag'},
    {'name': 'taskbuffer', 'code': 'taskbuffererrorcode', 'diag': 'taskbuffererrordiag'},
]

FAULTY_STATUSES = ('failed', 'cancelled', 'closed')

# Expanded field list for single-job deep study
STUDY_FIELDS = [
    # Identity
    'pandaid', 'jeditaskid', 'reqid', 'jobname', 'produsername', 'jobstatus',
    # Execution
    'computingsite', 'computingelement', 'transformation', 'processingtype',
    'creationtime', 'starttime', 'endtime', 'modificationtime',
    'corecount', 'actualcorecount', 'nevents',
    # Resources
    'maxrss', 'maxpss', 'maxvmem', 'maxswap', 'maxwalltime',
    'cpuconsumptiontime', 'cpuconsumptionunit',
    # I/O
    'inputfilebytes', 'ninputfiles', 'ninputdatafiles',
    'outputfilebytes', 'noutputdatafiles',
    'destinationdblock', 'destinationse',
    # Pilot / batch
    'pilotid', 'pilottiming', 'batchid',
    'container_name', 'specialhandling', 'commandtopilot',
    # All error fields
    'brokerageerrorcode', 'brokerageerrordiag',
    'ddmerrorcode', 'ddmerrordiag',
    'exeerrorcode', 'exeerrordiag',
    'jobdispatchererrorcode', 'jobdispatchererrordiag',
    'piloterrorcode', 'piloterrordiag',
    'superrorcode', 'superrordiag',
    'taskbuffererrorcode', 'taskbuffererrordiag',
    'transexitcode',
    # Metadata
    'jobmetrics', 'metadata',
]

# File table fields for study_job
FILE_FIELDS = [
    'lfn', 'type', 'guid', 'scope', 'fsize', 'status',
    'dataset', 'destinationdblock', 'checksum',
]

# Task field lists

TASK_LIST_FIELDS = [
    'jeditaskid', 'taskname', 'status', 'username',
    'creationdate', 'starttime', 'endtime', 'modificationtime',
    'reqid', 'processingtype', 'transpath',
    'progress', 'failurerate', 'errordialog',
    'site', 'corecount', 'taskpriority', 'currentpriority',
    'gshare', 'attemptnr', 'parent_tid', 'workinggroup',
]

# State-color maps — imported verbatim from PanDA BigMon
# (panda-bigmon-core/core/static/js/draw-plots-c3.js: task_state_colors /
# job_state_colors). BigMon tuned these over years; staying consistent so
# operators reading both monitors see the same palette for the same states.
# Keys are lowercase; callers should lowercase before lookup.
TASK_STATE_COLORS = {
    'done':        '#165616',  # dark green (terminal success)
    'finished':    '#207f20',  # green
    'running':     '#47D147',  # bright green (in-flight)
    'waiting':     '#c7c7c7',  # light gray
    'assigning':   '#099999',  # teal
    'exhausted':   '#e67300',  # orange
    'paused':      '#808080',  # gray
    'throttled':   '#FF9933',  # orange
    'pending':     '#deb900',  # amber
    'ready':       '#099999',  # teal
    'registered':  '#4a4a4a',  # dark gray
    'scouting':    '#addf80',  # light green
    'scouted':     '#addf80',
    'toabort':     '#ff9896',  # salmon
    'aborting':    '#FF8174',
    'aborted':     '#FF8174',
    'failed':      '#ff0000',  # red
    'broken':      '#b22222',  # firebrick
    'passed':      '#1a1a1a',  # near-black
    'defined':     '#2174bb',  # blue
    'remaining':   '#2174bb',
    'rerefine':    '#4a4a4a',
    'prepared':    '#4a4a4a',
}

JOB_STATE_COLORS = {
    'finished':     '#165616',  # dark green (terminal success, matches task 'done')
    'merging':      '#207f20',
    'running':      '#47D147',
    'starting':     '#addf80',
    'transferring': '#DBF1C6',
    'pending':      '#c7c7c7',
    'defined':      '#2174bb',
    'assigning':    '#099999',
    'activated':    '#3b8e67',
    'cancelled':    '#e67300',
    'throttled':    '#FF9933',
    'holding':      '#deb900',
    'sent':         '#FFD65D',
    'waiting':      '#808080',
    'closed':       '#4a4a4a',
    'failed':       '#ff0000',
    'broken':       '#b22222',
}

# Per-task job-count categorization.
# Bucketing of jobstatus values aggregated from jobsactive4 + jobsarchived4.
# The three returned counts (nactive, nfinished, nfailed) are what an alarm
# engine or dashboard needs to reason about task health. Cancelled and closed
# are deliberately excluded — operators know when they cancel; alarms surface
# what they don't know.
JOB_STATUS_CATEGORIES = {
    'active': (
        'defined', 'waiting', 'assigned', 'activated', 'sent',
        'starting', 'running', 'holding', 'transferring', 'merging',
    ),
    'finished': ('finished',),
    'failed': ('failed',),
}
