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

## External EVGEN Inputs

The example above is the pure MC-generation case (`noInput=True`). PCS also
needs to submit production tasks that consume **externally supplied EVGEN
files** — generator-level samples produced by PWGs/DSCs and described to PCS
as a CSV manifest (or other external source). See
[PCS_DATASET_REQUEST_WORKFLOW.md](PCS_DATASET_REQUEST_WORKFLOW.md) for the
PCS-side dataset model.

### Mode summary

| Mode | When | `noInput` | How payload sees the input |
|------|------|-----------|----------------------------|
| Generation-only | No external EVGEN; payload generates events | `True` | n/a |
| External EVGEN — payload-staged | External CSV manifest names the input files | `True` | `CSV_FILE=<location>` env var; payload script downloads and stages the listed files at runtime |
| External EVGEN — Rucio input | (Future) input is a registered Rucio dataset | `False` | JEDI drives input via standard `pfnList`/dataset reference |

Only the first two modes are in scope for the current PCS implementation.
Rucio-managed input is the natural follow-on once externally supplied EVGEN
files are registered as Rucio datasets, but it is deferred — moving from
payload-staged to Rucio-driven input is a localized later change.

### Payload-staged external EVGEN

This matches the existing condor-side wrapper convention: PCS already passes
`CSV_FILE=<csv_path>` as an environment variable to the condor job, and the
payload's `run.sh` reads it. We carry the same convention into JEDI.

The PCS source for the value is the linked `Dataset.metadata.source.location`
on the input dataset (kind `csv_manifest`), exposed via
`ProdTask.input_source_location`.

`build_task_params(task)` injects the value into the **payload command
string** in `jobParameters[0]['value']` when the task has an external input
source. The job dictionary keeps `noInput=True` and the rest of the
generation-mode shape — the only change is one extra `CSV_FILE=...` env in the
constant string. Example:

```python
'jobParameters': [
    {
        'type': 'constant',
        'value': (
            'EBEAM=10 PBEAM=100 '
            'DETECTOR_VERSION=26.02.0 DETECTOR_CONFIG=epic_craterlake '
            'JUG_XL_TAG=26.02.0-stable '
            'CSV_FILE=campaigns/26.02.0/dis_nc_10x100.csv '   # ← only this line is added
            'COPYRECO=true COPYFULL=false COPYLOG=true '
            './run.sh'
        ),
    },
    # output template unchanged
]
```

This is **not** a JEDI parameter. It is an env var the payload reads. JEDI
sees an opaque constant string and stages it into each job's command line as
written.

### What this does *not* require

- No new JEDI fields. `pfnList`, input-dataset references, and `nFilesPerJob`
  semantics for inputs all stay untouched.
- No GenTaskRefiner behavior change.
- No PanDA-team confirmation beyond what the all-`noInput` plan already
  needed.

The PCS-side mechanism is the only change: `ProdTask.input_dataset` (FK to a
`Dataset(stage=evgen, source.kind=csv_manifest)`) replaces the legacy
`ProdTask.csv_file` string as the source of truth, and `build_task_params`
reads from it.

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

## Implementation status (2026-06-03)

The **live** submission path is **prun via the prod-ops agent**, not the direct
`Client.insertTaskParams` of Phase 2. `build_panda_command` (`commands.py`) emits
the `prun` command; the agent's `submit_task` doer (`scripts/submit-prod-task.py`)
fetches it from `/pcs/api/prod-tasks/command/?fmt=panda` and runs it
non-interactively under the operator's cached OIDC token, then records the
jediTaskID via `/pcs/api/prod-tasks/record-submission/`. This matches the
validated manual recipe (jediTaskID 36439, see [EPICPROD_OPS.md](EPICPROD_OPS.md)).

`build_task_params` (the `taskParamMap`, `?fmt=jedi`) is **preview-only** today —
rendered in the compose UI but not submitted. Phase 2 (`pcs/submission.py`,
direct `insertTaskParams`) is not built; if adopted it must reconcile with the
prun path's defaults (e.g. `vo`: `eic` in `build_task_params` vs `wlcg` in
`build_panda_command`). Phases 3-4 (status polling) remain design.

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

## Submitting from the CLI

Today, `pcs-task-cmd` (documented in [PCS.md](PCS.md)) can emit the `taskParamMap` JSON for any task. Operators with a valid PanDA auth context (x509 proxy or OIDC token) can pipe it straight into `Client.insertTaskParams()`:

```bash
pcs-task-cmd <task_name> --format jedi | python -c '
import json, sys
from pandaclient import Client
print(Client.insertTaskParams(json.load(sys.stdin)))
'
```

This is the intended test-phase submission path. Server-side submission from swf-monitor is blocked on the OIDC service account listed below.

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
