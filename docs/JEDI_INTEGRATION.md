# JEDI Integration — Direct Task Submission from PCS

## Overview

PCS (Physics Configuration System) currently composes physics, event generation, simulation, and reconstruction tags into fully specified production tasks, then generates `prun` CLI commands and Condor submit scripts as text. The next step is to **submit tasks directly to JEDI via the PanDA Python API**, bypassing script generation entirely.

This document describes the integration design: how PCS task parameters map to JEDI's `taskParamMap`, the submission flow, and what infrastructure support is needed from PanDA.

**Approach:** Direct API submission. PCS owns the full task specification. JEDI's existing `GenTaskRefiner` handles the task — no custom server-side plugin required.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  PCS (swf-monitor)                              │
│                                                 │
│  PhysicsTag ─┐                                  │
│  EvgenTag   ─┼─► Dataset ─┐                     │
│  SimuTag    ─┘             ├─► ProdTask          │
│  RecoTag   ─┘   ProdConfig┘     │               │
│                                  │               │
│                   build_task_params(task)        │
│                          │                      │
│                          ▼                      │
│                   taskParamMap (dict)            │
│                          │                      │
│                 submit_to_jedi(task)             │
│                          │                      │
└──────────────────────────┼──────────────────────┘
                           │ Client.insertTaskParams()
                           ▼
┌─────────────────────────────────────────────────┐
│  PanDA Server                                   │
│                                                 │
│  POST /api/v1/task/submit                       │
│       │                                         │
│       ▼                                         │
│  TaskBuffer.insertTaskParamsPanda()             │
│       │  stores task in DB, state = "defined"   │
│       ▼                                         │
│  JEDI TaskRefiner daemon                        │
│       │  selects GenTaskRefiner via VO config   │
│       ▼                                         │
│  GenTaskRefiner.extractCommon()                 │
│  GenTaskRefiner.doRefine()                      │
│       │  creates JediTaskSpec + dataset specs   │
│       ▼                                         │
│  ContentsFeeder → JobGenerator → JobBroker      │
│       │  breaks task into jobs, assigns sites   │
│       ▼                                         │
│  Jobs dispatched to Pilot                       │
└─────────────────────────────────────────────────┘
```

## PCS-to-JEDI Field Mapping

### Task Identity

| JEDI Parameter | PCS Source | Notes |
|---------------|-----------|-------|
| `taskName` | `dataset.task_name` | Dataset name without `.bN` block suffix |
| `userName` | `task.created_by` | PCS user who created the task |
| `vo` | `'eic'` | Virtual organization |
| `workingGroup` | `config.panda_working_group` | e.g. `'EIC'` |
| `campaign` | Derived from detector version | e.g. `'26.02.0'` |

### Processing Definition

| JEDI Parameter | PCS Source | Notes |
|---------------|-----------|-------|
| `prodSourceLabel` | `config.data['prod_source_label']` | `'managed'` for production, `'test'` for testing |
| `taskType` | `'production'` | Fixed for PCS production tasks |
| `processingType` | `config.data['processing_type']` | e.g. `'epicproduction'` |
| `taskPriority` | `config.data` or default | 0-1000, production typically 900 |
| `transPath` | `config.data['transformation']` | Payload executable or TRF URL |
| `transUses` | `''` | Not used for containerized jobs |
| `transHome` | `''` | Not used for containerized jobs |
| `architecture` | `''` | Empty string — container handles platform |
| `container_name` | `config.container_image` | Singularity/Docker image reference |

### Job Splitting

| JEDI Parameter | PCS Source | Notes |
|---------------|-----------|-------|
| `nEventsPerJob` | `config.data['events_per_job']` | Events per individual job |
| `nEvents` | `config.events_per_task` | Total events for the task |
| `nFiles` | `config.data['n_jobs']` | When using noInput, this controls job count |
| `nFilesPerJob` | `config.data['files_per_job']` | Input files per job (default 1) |
| `noInput` | `True` | MC generation has no input dataset |
| `coreCount` | `config.data['corecount']` | CPU cores per job (default 1) |
| `walltime` | Derived from `config.target_hours_per_job` | In seconds for JEDI |
| `ramCount` | `config.data` or GenTaskRefiner default | MB per core (default 2000) |

### Site Selection

| JEDI Parameter | PCS Source | Notes |
|---------------|-----------|-------|
| `site` | `config.panda_site` | PanDA queue name, e.g. `'BNL_EPIC_PROD_1'` |
| `cloud` | `config.panda_working_group` or `'US'` | GenTaskRefiner copies workingGroup to cloud |

### Output Datasets

| JEDI Parameter | PCS Source | Notes |
|---------------|-----------|-------|
| `log` | Built from `dataset.did` | Log dataset template |
| `jobParameters` | Built from config | Execution command + output file templates |

### Flags

| JEDI Parameter | PCS Source | Notes |
|---------------|-----------|-------|
| `skipScout` | `config.data['skip_scout']` | Skip scout jobs if True |
| `disableAutoRetry` | `config.data` | Optional |
| `useRucio` | `config.use_rucio` | Whether to register outputs in Rucio |

## Example: taskParamMap Built from PCS

Given a ProdTask with:
- Dataset: `group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1` (DIS NC 10x100)
- ProdConfig: container image, 100 events/job, 1000 total events, 1 core

The `build_task_params(task)` function would produce:

```python
taskParamMap = {
    # Identity
    'taskName': 'group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1',
    'userName': 'wenaus',
    'vo': 'eic',
    'workingGroup': 'EIC',
    'campaign': '26.02.0',

    # Processing
    'prodSourceLabel': 'managed',
    'taskType': 'production',
    'processingType': 'epicproduction',
    'taskPriority': 900,

    # Executable (containerized)
    'transPath': 'https://pandaserver-doma.cern.ch/trf/user/runGen-00-00-02',
    'transUses': '',
    'transHome': '',
    'architecture': '',
    'container_name': 'docker://eicweb/jug_xl:26.02.0-stable',

    # Splitting
    'noInput': True,
    'nFiles': 10,           # number of jobs
    'nFilesPerJob': 1,
    'nEventsPerJob': 100,
    'coreCount': 1,
    'ramCount': 4000,
    'ramUnit': 'MBPerCore',

    # Site
    'site': 'BNL_EPIC_PROD_1',
    'cloud': 'EIC',

    # Log output
    'log': {
        'dataset': 'group.EIC:group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1.log',
        'type': 'template',
        'param_type': 'log',
        'token': 'local',
        'destination': 'local',
        'value': 'group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1.log.${SN}.log.tgz',
    },

    # Job parameters: execution command + output file spec
    'jobParameters': [
        {
            'type': 'constant',
            'value': (
                'EBEAM=10 PBEAM=100 '
                'DETECTOR_VERSION=26.02.0 DETECTOR_CONFIG=epic_craterlake '
                'JUG_XL_TAG=26.02.0-stable '
                'COPYRECO=true COPYFULL=false COPYLOG=true '
                './run.sh'
            ),
        },
        {
            'type': 'template',
            'param_type': 'output',
            'token': 'local',
            'destination': 'local',
            'dataset': 'group.EIC:group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1',
            'value': 'group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1.${SN}.root',
            'offset': 1000,
        },
    ],
}
```

## GenTaskRefiner Behavior

When JEDI processes this task, `GenTaskRefiner` (61 lines, `panda-server/pandajedi/jedirefine/GenTaskRefiner.py`) applies these defaults:

1. **`cloud`** — if absent, copies from `workingGroup` (so `cloud='EIC'` from `workingGroup='EIC'`)
2. **`transPath`** — defaults to `runGen-00-00-02` TRF if not set (we set it explicitly)
3. **`ramCount`** — defaults to 2000 MB if not set
4. **`pushStatusChanges`** — defaults to True (status updates via message queue)
5. **`messageDriven`** — defaults to True
6. **`cloudAsVO`** — always set to True (cloud field used as VO for brokerage)
7. **Dataset templates** — instantiated per-site if DDM interface is available

The `GenJobBroker` then handles site selection using the simplified non-ATLAS brokerage logic: filter by queue status, disk space, walltime constraints, then select.

## Implementation Plan

### Phase 1: build_task_params() (commands.py)

Add a new function alongside the existing `build_condor_command()` and `build_panda_command()`:

```python
def build_task_params(task):
    """
    Build a JEDI taskParamMap dict from a ProdTask.

    Returns the dict that can be passed directly to
    pandaclient.Client.insertTaskParams() for JEDI submission.
    """
```

This function reads the same ProdTask → ProdConfig → Dataset → Tags chain but produces a dict instead of a CLI string. The `ProdTask.generate_commands()` method should also call this and store the result (JSON) for review before submission.

### Phase 2: submit_to_jedi() (new module: pcs/submission.py)

```python
def submit_to_jedi(task):
    """
    Submit a ProdTask to JEDI via PanDA API.

    Returns (status, jedi_task_id) on success.
    Updates task.panda_task_id and task.status.
    """
```

This calls `Client.insertTaskParams(task_params)` and handles the response. Authentication uses OIDC (`PANDA_AUTH=oidc`, `PANDA_AUTH_VO=eic`).

### Phase 3: UI Integration

- Add a "Submit to JEDI" button on the ProdTask detail page (alongside existing command display)
- Show the taskParamMap as formatted JSON for review before submission
- After submission, display the JEDI task ID with link to ePIC production monitoring
- Status tracking via `Client.getTaskStatus(jedi_task_id)`

### Phase 4: Task Monitoring

- Poll JEDI task status and update ProdTask.status accordingly
- ePIC prod monitoring views for task and job info, and info via MCP tools
- Surface errors via the existing PanDA MCP tools

## Infrastructure: What We Know

- **VO**: `eic`
- **Queues**: 13 EIC queues online (BNL_EPIC_PROD_1, BNL_OSG_EPIC_PROD_1, NERSC_Perlmutter_epic, E1_BNL, E1_JLAB, etc.). All support Apptainer containers.
- **Auth**: OIDC with `PANDA_AUTH=oidc`, `PANDA_AUTH_VO=eic`
- **Output**: Rucio integration available; `token='local'` / `destination='local'` for local staging

## What PanDA Team Needs to Confirm

1. **GenTaskRefiner registration** for `eic:managed` in `panda_jedi.cfg`
2. **OIDC service account** setup for non-interactive programmatic submission from our production server
3. **`transPath`** — is the GenTaskRefiner default TRF appropriate for containerized EIC jobs, or should we specify our own?

## Key References

### PanDA Documentation (panda-docs repo)
- [Task Parameters](../../../panda-docs/docs/source/advanced/task_params.rst) — splitRule codes and parameter priority
- [JEDI Architecture](../../../panda-docs/docs/source/architecture/jedi.rst) — task flow, agents, state machines
- [Client API](../../../panda-docs/docs/source/client/panda-client.rst) — Python API setup and usage
- [Admin Guide](../../../panda-docs/docs/source/admin_guide/admin_guide.rst) — GenTaskRefiner config examples

### PanDA Source Code (cloned in github/)
- `panda-server/pandajedi/jedirefine/GenTaskRefiner.py` — the refiner our tasks will use
- `panda-server/pandajedi/jedirefine/TaskRefinerBase.py` — extractCommon() parameter processing
- `panda-server/pandajedi/jeditest/addNonAtlasTask.py` — non-ATLAS submission example
- `panda-client/pandaclient/example_task.py` — client-side task dict example
- `panda-client/pandaclient/panda_api.py` — `submit_task()` high-level API
- `panda-client/pandaclient/Client.py:1304` — `insertTaskParams()` implementation

### PCS Source Code (swf-monitor)
- `src/pcs/models.py` — ProdTask, ProdConfig, Dataset, tag models
- `src/pcs/commands.py` — current command generation (to be extended)
- `docs/PCS.md` — PCS documentation
