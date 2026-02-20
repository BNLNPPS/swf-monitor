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

# Task field lists

TASK_LIST_FIELDS = [
    'jeditaskid', 'taskname', 'status', 'username',
    'creationdate', 'starttime', 'endtime', 'modificationtime',
    'reqid', 'processingtype', 'transpath',
    'progress', 'failurerate', 'errordialog',
    'site', 'corecount', 'taskpriority', 'currentpriority',
    'gshare', 'attemptnr', 'parent_tid', 'workinggroup',
]
