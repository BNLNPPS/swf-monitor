# PCS Production Planning Workflow

This note describes the PCS-based workflow for ePIC production
dataset requests. It is both an implementation guide and a
summary to confirm that it matches the production
planning workflow foreseen by the production team and taking shape in `eic/epic-prod`.

## Summary

PCS should be the authoritative record for production 
requests, which take form as
task specifications composed from dataset, tag, and production configuration specs.
The tasks are also the source of record for downstream production state as
production progresses. Mattermost/pandabot is a two-way conversational
interface between users and PCS. The `eic/epic-prod` issue/PR/Jekyll workflow is
the public catalogue projection of production plans as expressed in PCS data,
with PCS the source of truth.

The current production reality starts from generator-level files supplied by
PWGs or DSCs, input as CSV file spec manifests. PCS must represent
this present reality, describing
those externally supplied inputs explicitly, while also providing a clean path to a
future mode where EVGEN is run as an internal production stage.

## Current Situation

Sakib has described and prototyped this public intake path:

1. A requester submits a dataset request through a GitHub issue template or,
   later, through Mattermost/PanDAbot as dialog front end to issue creation.
2. A GitHub Action triggered by the issue creation appends a row to `eic/epic-prod`:
   `docs/_data/datasets.csv`.
3. A pull request is opened for review.
4. Once merged, the Jekyll-generated page at the campaign datasets documentation displays
   the new request.

The public CSV currently stores fields such as:

- DSC or PWG
- Dataset Path
- Generator/Dataset Version
- Number of Events
- Background
- New Request
- Pre-TDR Use
- Early Science Use
- Other Use
- Description
- Priority
- Issue

This is metadata that maps onto the PCS components that
compose into either a fully or partially specified production task. 

At present, `Generator/Dataset Version` is a loose string,
not a structured PCS EvGen tag, and `Dataset Path` is an external data source or
manifest, not a tag attribute.

## PCS Authority

PCS is designed to be the authoritative source and record for task configuration:

- request state
- inferred and confirmed metadata
- associated physics, EvGen, simulation, and reconstruction tags
- input and output datasets
- production configuration
- workflow mode and stage structure
- validation status
- public catalogue issue, PR, row, and page references
- user comments
- downstream production status and task IDs

PCS definition and composition into tasks is currently based in a web interface.
Intake is via PCS REST. Bots, scripts, and the web UI all converge on the
same REST surface; there is one contract, with multiple front-ends.

- **Bots trigger; they do not mediate.** PanDAbot receives a Mattermost
  event, then calls a PCS MCP tool. Bots do not construct REST queries
  themselves and do not embed PCS logic. Intake decisions, validation,
  and persistence live behind REST.
- **MCP wraps REST.** Each MCP tool is a one-line adapter over a REST
  endpoint, so adding a new intake operation is "add the REST endpoint,
  expose an MCP tool" — never the reverse.
- **Scripts and other automation** call REST directly, with the same
  contract.
- **GitHub issue creation, when needed,** is performed by PCS server-side
  on receipt of a 'go' from the bot/user via REST — programmatically
  precise, so traceability and review are preserved.

User-supplied comments arrive through whichever front-end the user chose
(bot, web, script). PCS records them as part of the request history via
the same REST endpoint. The bot's job is to relay PCS responses
(questions, inferred values, short option lists, validation errors)
back to the user; PCS's job is to compute them.

The REST surface itself is listed under "REST Intake Surface" below.

## Model Direction

Tags remain reusable attribute sets. They do not carry concrete file paths or
manifests. A concrete data sample is a dataset, described by one or more tags.

PCS should keep one generalized dataset model and extend it to cover externally
supplied data as well as internally produced data. The key additions are:

- stage or role, e.g. `evgen`, `simu`, `reco`, `full`, `log`
- source kind and source location, e.g. CSV manifest, path, URL, Rucio DID, file list

The present externally supplied EVGEN files are then ordinary PCS datasets with
`stage=evgen` and `source.kind=csv_manifest` or another external source kind.
They are described by PCS physics and EvGen tags, but their file/path/CSV
source is dataset metadata, not tag metadata.
Future PCS/PanDA-produced EVGEN outputs are also ordinary datasets, with
`stage=evgen` and an internal production source when that exists.

Interim implementation: externally supplied EVGEN inputs are represented in the
existing `Dataset.metadata` JSON rather than by new database columns. The
metadata convention is:

```json
{
  "stage": "evgen",
  "source": {
    "kind": "csv_manifest",
    "location": "path/to/input.csv"
  }
}
```

This keeps the external-input capability lightweight and transitional while
preserving the path to a later first-class workflow/stage model for EVGEN run
inside PCS/PanDA.

Production tasks should compose lists of datasets rather than a single dataset:

```text
ProdTask
  prod_config
  input_datasets[]
  output_datasets[]
  overrides
  status and submission tracking
```

The current `ProdTask.dataset` is really the output dataset. The current
`ProdTask.csv_file` is really source metadata for an external input dataset.
Both should migrate in that direction while preserving backward compatibility.

`ProdConfig` should carry workflow-mode defaults: external EVGEN input, internal
EVGEN stage execution, stage template, transformation or executable, splitting
strategy, resources, and site/queue defaults. Task-level `overrides` remain the
last-mile specialization mechanism for a reusable production config.

## Workflow Modes

The same production intent should be expressible in two modes:

```text
external EVGEN dataset -> simulation/reconstruction -> output dataset(s)
```

and later:

```text
internal EVGEN stage -> simulation stage -> reconstruction stage -> output dataset(s)
```

The internal EVGEN case should still be one PCS production task or request,
with an internal workflow graph describing the stages. It should not require a
graph of multiple top-level PCS tasks. The model should also allow both modes
to be run and compared under the same physics, EvGen, simulation, and
reconstruction metadata.

## Lifecycle

The public catalogue publication state is not production readiness. Sakib's
current issue fields are sufficient to create a public planning row and a
partial PCS request, but not a fully specified PCS production task.

The PCS lifecycle should therefore be explicit:

```text
requested
needs_metadata
needs_tag_mapping
needs_input_validation
planned
ready_for_operator_review
ready_to_submit
submitted
```

PCS should infer likely values where possible, present short option lists via
the bot, record user comments, and support operator completion through
templates, defaults, and direct metadata entry. Readiness checks should validate
paths, CSV manifests, file readability where possible, event counts, tag
mapping, production config, and public catalogue projection.

PCS should store the public catalogue mapping internally:

```text
public_catalog_repo = eic/epic-prod
public_catalog_issue
public_catalog_pr
public_catalog_row_index
public_catalog_csv_path = docs/_data/datasets.csv
public_catalog_row_key = Issue=<issue number>
public_catalog_page_url
public_catalog_commit_sha
```

The GitHub issue number is the durable update key. The visible row index is a
useful human locator, but advisory.

## REST Intake Surface

All intake — from bots (via MCP), scripts, and the web UI — goes through
the same REST endpoints. Most are already in place; the gaps are the
high-level idempotent intake calls that take a request payload (CSV
manifest, GitHub issue, etc.) and return a Dataset DID or ProdTask name.

| Method | Endpoint | Purpose |
|---|---|---|
| POST   | `/pcs/api/datasets/`                          | Generic create. Body carries `metadata` (validated for external source.kind/location). |
| POST   | `/pcs/api/datasets/intake/`                   | Idempotent: given a CSV-manifest location (+ optional tag handles), find-or-create the external EVGEN Dataset, return its DID. |
| POST   | `/pcs/api/prod-tasks/`                        | Generic create. |
| POST   | `/pcs/api/prod-tasks/intake/`                 | Idempotent on a request key (e.g. `epic-prod#<issue>` or `csv_path+row_key`): create a draft ProdTask, ensure linked input Dataset(s), persist `public_catalog_*` mapping fields in `overrides`, return the task. |
| POST   | `/pcs/api/prod-tasks/<pk>/link-input/`        | Link an existing Dataset as input by DID (writes `overrides.input_dataset_did(s)`). Sugar over PATCH. |
| POST   | `/pcs/api/prod-tasks/<pk>/set-status/`        | Lifecycle transition with rule enforcement (e.g. only `ready → submitted`). |
| POST   | `/pcs/api/prod-tasks/record-submission/?name=`| Record JEDI submission outcome (`panda_task_id`, `status='submitted'`). Rejects if `panda_task_id` already set, or `status != 'ready'`. |
| GET    | `/pcs/api/prod-tasks/command/?name=&fmt=`     | Submission artifact — `condor`/`panda`/`jedi`/`dump`. |
| GET    | `/pcs/api/{datasets,prod-tasks}/`             | List with filters (`stage`, `source_kind`, `status`, `public_catalog_issue`, …). |

### Idempotency keys

The two `intake/` endpoints are idempotent and require a stable key in
the request body:

- `datasets/intake/` keys on `source.location` (+ `source.kind`, default
  `csv_manifest`). Repeated calls with the same location return the same
  Dataset row.
- `prod-tasks/intake/` keys on either:
  - `public_catalog_issue` when the request originated from a GitHub
    issue, or
  - `(public_catalog_csv_path, public_catalog_row_key)` when the request
    is identified by a row in `datasets.csv`.

Repeated calls with the same key return the same ProdTask row, never
duplicate. New input fields are merged into the existing draft (until
the task is locked or submitted).

### MCP wrappers

Each MCP tool is a one-line adapter over the corresponding REST call.
This way, adding a new intake operation is "add the REST endpoint,
expose an MCP tool" — never the reverse.

- `pcs_dataset_intake(source_location, source_kind='csv_manifest', tags=...)` → DID
- `pcs_prodtask_intake(request_payload)` → task name
- `pcs_prodtask_link_input(task_name, did)`
- `pcs_prodtask_set_status(task_name, status)`
- `pcs_prodtask_submit(task_name)` → jediTaskID
- `pcs_prodtask_get(task_name)` / `pcs_prodtask_list(filters)`

### Public catalogue mapping fields

Stored in `ProdTask.overrides` under reserved keys (no schema columns
needed for the interim model):

`public_catalog_repo`, `public_catalog_issue`, `public_catalog_pr`,
`public_catalog_row_index`, `public_catalog_csv_path`,
`public_catalog_row_key`, `public_catalog_page_url`,
`public_catalog_commit_sha`.

## Dynamic Public Catalog

The present GitHub/Jekyll public catalog webpage provides a static snapshot of production requests, but does not offer dynamic modification: cloning of established requests into new campaigns, modification of requests and injection into campaigns, adjustment of priorities, withdrawal of requests, and other changes, all of them based on authenticated users with particular production system rights.

PCS already has dynamic listings and edit/copy/delete/update extensible functionality in its web interface, including the task interface. PCS dynamic changes should still preserve traceability/audit/review, perhaps by GitHub PRs at first and later by PCS-native audit logs and approval workflows. This dynamic interface will be developed as a candidate for adoption as the official public catalog interface, once PCS and automated production is proven and established on an ePIC owned server. 

## Implementation Plan

1. Extend `Dataset` with stage and source metadata.
2. Add task-dataset relations for input, output, and intermediate dataset lists.
3. Migrate current `ProdTask.dataset` semantics to an output dataset relation.
4. Move current `ProdTask.csv_file` semantics to external input dataset source
   metadata, with backward compatibility during transition.
5. Add workflow-mode/template fields to `ProdConfig`.
6. Add the REST intake endpoints listed under "REST Intake Surface"
   (`datasets/intake/`, `prod-tasks/intake/`, `link-input/`, the
   lifecycle gates on `set-status/` and `record-submission/`).
   Expose each as a one-line MCP wrapper so PanDAbot can trigger
   intake, status transitions, comment append, catalogue-row preview,
   and PCS-driven GitHub issue creation by calling MCP only — never
   composing REST queries itself.
7. Continue to develop the integrated automated production workflow from user input to
   running task, including a dynamic web interface providing documentation and
   flexible interaction with and control of the automated production system,
   as well as the mattermost/bot interface.
