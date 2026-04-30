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
The plan to support bot submission is as follows.

- the PCS MCP service is extended to support injection of PCS data, task composition and task submission requests.
- the PCS MCP service supports the bot acting as mediator and Q&A driver to gather from the user the information needed to constitute a sufficiently complete request to create a draft PCS task and project it to the public catalogue. PanDAbot should submit user-provided request data to PCS, then relay PCS questions, inferred values, short option lists, validation errors, and comments
back to the user. Comments supplied through the bot should be saved in PCS as
part of the request history.
- PCS informs the user via the bot that the point of readiness has been reached, and the user then instructs the bot to proceed with issue creation.
- the issue creation should be programmatically precise, and so is performed not by the bot but by PCS, once the bot passes the user's 'go' on issue creation to PCS via MCP.
- the GitHub path via the existing
GitHub issue/PR path then proceeds as now, so traceability and review are preserved.

## Model Direction

Tags remain reusable attribute sets. They do not carry concrete file paths or
manifests. A concrete data sample is a dataset, described by one or more tags.

PCS should keep one generalized dataset model and extend it to cover externally
supplied data as well as internally produced data. The key additions are:

- stage or role, e.g. `evgen`, `simu`, `reco`, `full`, `log`
- external flag
- source kind and source location, e.g. path, CSV manifest, URL, Rucio DID, file list
- manifest reference or content hash where useful
- provider or requesting PWG/DSC
- provenance and validation status
- public catalogue references where applicable

The present externally supplied EVGEN files are then ordinary PCS datasets with
`stage=evgen` and `external=true`. They are described by PCS physics and EvGen
tags, but their file/path/CSV source is dataset metadata, not tag metadata.
Future PCS/PanDA-produced EVGEN outputs are also ordinary datasets, with
`stage=evgen` and `external=false`.

Interim implementation: externally supplied EVGEN inputs are represented in the
existing `Dataset.metadata` JSON rather than by new database columns. The
metadata convention is:

```json
{
  "stage": "evgen",
  "external": true,
  "source": {
    "kind": "csv_manifest",
    "location": "path/to/input.csv",
    "hash": null
  },
  "provider": {
    "group": "PWG or DSC name",
    "contact": null
  },
  "provenance": {
    "status": "declared",
    "notes": "Supplied by PWG; PCS has not independently verified physics content."
  },
  "validation": {
    "status": "not_checked",
    "checked_at": null,
    "messages": []
  },
  "public_catalog": {}
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

## Dynamic Public Catalog

The present GitHub/Jekyll public catalog webpage provides a static snapshot of production requests, but does not offer dynamic modification: cloning of established requests into new campaigns, modification of requests and injection into campaigns, adjustment of priorities, withdrawal of requests, and other changes, all of them based on authenticated users with particular production system rights.

PCS already has dynamic listings and edit/copy/delete/update extensible functionality in its web interface, including the task interface. PCS dynamic changes should still preserve traceability/audit/review, perhaps by GitHub PRs at first and later by PCS-native audit logs and approval workflows. This dynamic interface will be developed as a candidate for adoption as the official public catalog interface, once PCS and automated production is proven and established on an ePIC owned server. 

## Implementation Plan

1. Extend `Dataset` with stage, external/source, provenance, validation, and
   public-catalogue reference metadata.
2. Add task-dataset relations for input, output, and intermediate dataset lists.
3. Migrate current `ProdTask.dataset` semantics to an output dataset relation.
4. Move current `ProdTask.csv_file` semantics to external input dataset source
   metadata, with backward compatibility during transition.
5. Add workflow-mode/template fields to `ProdConfig`.
6. Extend PCS MCP tools so PanDAbot can create draft requests, add comments,
   answer missing-field prompts, preview catalogue rows, and pass the user's
   approval for PCS-driven GitHub issue creation.
7. Continue to develop the integrated automated production workflow from user input to
   running task, including a dynamic web interface providing documentation and
   flexible interaction with and control of the automated production system,
   as well as the mattermost/bot interface.
