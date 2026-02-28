# ePIC Metadata Interface (EMI)

## Overview

EMI is a metadata management system for ePIC production at the Electron Ion Collider. It provides a central service to define, browse, reuse, and compose metadata configurations that drive Monte Carlo simulation campaigns.

Production metadata is organized as **tags** — named, versioned parameter sets that capture physics process definitions, event generation settings, simulation configurations, and reconstruction options. Tags are composed into **datasets** with standardized names suitable for Rucio data management. **Production configs** capture operational settings for job submission: background mixing, output control, software stack, resource targets, and PanDA/Rucio overrides.

**Location:** Django app `emi` within swf-monitor
**Web UI:** `/swf-monitor/emi/`
**REST API:** `/swf-monitor/emi/api/`

## Concepts

### Tags

A tag is a named parameter set identified by a type prefix and number:

| Type | Prefix | Example | Description |
|------|--------|---------|-------------|
| Physics | p | p3001 | Physics process (beam energies, process type, cross-section) |
| EvGen | e | e1 | Event generation (signal frequency, generator settings) |
| Simulation | s | s1 | Detector simulation (sim version, backgrounds, digitization) |
| Reconstruction | r | r1 | Reconstruction (version, calibration, alignment) |

### Tag Lifecycle

Tags follow a two-state lifecycle:

```
draft  ──────►  locked
(editable)      (immutable, usable in datasets)
```

- **Draft**: Parameters can be edited. Cannot be used in datasets.
- **Locked**: Immutable. One-way transition from draft. Required for dataset creation. Ensures reproducibility — once a tag is used in production, its meaning never changes.

### Physics Categories

Physics tags use a hierarchical numbering scheme. Each physics area (DVCS, DIS, SIDIS, etc.) is assigned a single digit (1-9). Tag numbers within that area start at `digit * 1000 + 1`:

| Category | Digit | Tag Range |
|----------|-------|-----------|
| DVCS | 3 | p3001, p3002, p3003... |
| DIS | 4 | p4001, p4002, p4003... |

EvGen, Simulation, and Reconstruction tags simply increment from 1 (e1, e2, ... / s1, s2, ... / r1, r2, ...).

### Tag Number Allocation

All tag numbers are auto-assigned atomically:

- **Physics tags**: `MAX(tag_number) + 1` within the category, using `select_for_update()` for thread safety.
- **e/s/r tags**: Atomic increment of keys in `PersistentState.state_data` (`emi_next_evgen`, `emi_next_simu`, `emi_next_reco`).

Users never pick tag numbers manually — EMI assigns them.

### Datasets

A dataset composes four locked tags with detector version information into a standardized name:

```
{scope}.{detector_version}.{detector_config}.{physics_tag}.{evgen_tag}.{simu_tag}.{reco_tag}
```

Example:
```
group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1
```

The Rucio DID (Data Identifier) adds scope prefix and block suffix:
```
group.EIC:group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1.b1
```

Dataset names are validated to not exceed 255 characters (Rucio limit).

### Blocks

Rucio limits datasets to 100k files. EMI manages automatic subdivision:

- Block 1 (`.b1`) always exists, created with the dataset.
- When a dataset exceeds the file limit, add a new block — EMI creates `.b2`, `.b3`, etc.
- All blocks share the same base dataset name and tag composition.
- The `blocks` count is maintained across all rows of the same dataset.

### Production Configs

A production config is a reusable template capturing everything needed to build a submit command beyond what tags and datasets define:

- **Background mixing**: Enable/disable, cross section, EvtGen file
- **Output control**: Which output files to copy (reco, full, log), Rucio usage
- **Software stack**: JUG_XL tag, container image
- **Resource targets**: Target walltime per job, events per task
- **Condor template**: HTCondor submission template text
- **PanDA overrides**: Site, queue, working group, resource type (nullable — PanDA decides defaults)
- **Rucio overrides**: RSE, replication rules (nullable)

Production configs are **always mutable** — they are working templates, not reproducibility records. The PanDA task/job spec is the immutable record of what actually ran.

## Data Model

### Tables

| Table | Description |
|-------|-------------|
| `emi_physics_category` | Physics areas with digit-based numbering |
| `emi_physics_tag` | Physics process tags (p####) |
| `emi_evgen_tag` | Event generation tags (e#) |
| `emi_simu_tag` | Simulation tags (s#) |
| `emi_reco_tag` | Reconstruction tags (r#) |
| `emi_dataset` | Datasets with block management |
| `emi_prod_config` | Production configuration templates |

### Tag Parameter Schemas

Each tag type has required and optional parameter fields defined in `emi/schemas.py`:

**Physics (p):**
- Required: `process`, `beam_energy_electron`, `beam_energy_hadron`
- Optional: `crosssection`, `generator`, `luminosity`, `notes`

**EvGen (e):**
- Required: `signal_freq`, `signal_status`
- Optional: `generator_version`, `decay_mode`, `notes`

**Simulation (s):**
- Required: `detector_sim`, `sim_version`
- Optional: `background_config`, `digitization`, `notes`

**Reconstruction (r):**
- Required: `reco_version`, `reco_config`
- Optional: `calibration_tag`, `alignment_tag`, `notes`

Schemas are extensible — add fields to `TAG_SCHEMAS` in `schemas.py` without migration.

## REST API

Base URL: `/emi/api/`

All endpoints support JSON. No DELETE on tags or datasets — they are permanent. Prod configs support full CRUD.

### Physics Categories

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/physics-categories/` | List all categories with tag counts |
| POST | `/physics-categories/` | Create a category |
| GET | `/physics-categories/{digit}/` | Get category detail |
| PATCH | `/physics-categories/{digit}/` | Update category |

### Tags (same pattern for all four types)

Replace `{type}` with `physics-tags`, `evgen-tags`, `simu-tags`, or `reco-tags`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/{type}/` | List tags |
| POST | `/{type}/` | Create tag (number auto-assigned) |
| GET | `/{type}/{tag_number}/` | Get tag detail |
| PATCH | `/{type}/{tag_number}/` | Update tag (draft only) |
| POST | `/{type}/{tag_number}/lock/` | Lock tag (one-way) |

**POST physics-tags** requires `category` (digit). Other tag types do not.

**PATCH** returns 400 if the tag is locked.

### Datasets

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/datasets/` | List datasets |
| POST | `/datasets/` | Create dataset (all tags must be locked) |
| GET | `/datasets/{id}/` | Get dataset detail |
| POST | `/datasets/{id}/add-block/` | Add next block |

### Examples

Create a physics category:
```bash
curl -X POST /emi/api/physics-categories/ \
  -H "Content-Type: application/json" \
  -d '{"digit": 3, "name": "DVCS", "description": "Deeply Virtual Compton Scattering", "created_by": "torre"}'
```

Create a physics tag:
```bash
curl -X POST /emi/api/physics-tags/ \
  -H "Content-Type: application/json" \
  -d '{
    "category": 3,
    "description": "DVCS 10x100 GeV",
    "parameters": {
      "process": "DVCS",
      "beam_energy_electron": "10",
      "beam_energy_hadron": "100"
    },
    "created_by": "torre"
  }'
# Returns: {"tag_number": 3001, "tag_label": "p3001", ...}
```

Lock a tag:
```bash
curl -X POST /emi/api/physics-tags/3001/lock/
```

Create a dataset:
```bash
curl -X POST /emi/api/datasets/ \
  -H "Content-Type: application/json" \
  -d '{
    "scope": "group.EIC",
    "detector_version": "26.02.0",
    "detector_config": "epic_craterlake",
    "physics_tag": 1,
    "evgen_tag": 1,
    "simu_tag": 1,
    "reco_tag": 1,
    "created_by": "torre"
  }'
# Returns: {"did": "group.EIC:group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1.b1", ...}
```

Add a block when dataset exceeds 100k files:
```bash
curl -X POST /emi/api/datasets/1/add-block/
# Returns: {"did": "...b2", "block_num": 2, "blocks": 2, ...}
```

### Production Configs

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/prod-configs/` | List all prod configs |
| POST | `/prod-configs/` | Create a prod config |
| GET | `/prod-configs/{id}/` | Get config detail |
| PATCH | `/prod-configs/{id}/` | Update config |
| DELETE | `/prod-configs/{id}/` | Delete config |

Create a production config:
```bash
curl -X POST /emi/api/prod-configs/ \
  -H "Content-Type: application/json" \
  -d '{
    "name": "DVCS 10x100 standard",
    "description": "Standard DVCS production at 10x100 GeV",
    "bg_mixing": true,
    "bg_cross_section": "1.0e-3",
    "copy_reco": true,
    "copy_full": false,
    "copy_log": true,
    "use_rucio": true,
    "jug_xl_tag": "26.02.0-stable",
    "target_hours_per_job": 4.0,
    "events_per_task": 100000,
    "panda_working_group": "EIC",
    "created_by": "torre"
  }'
```

## Web UI

### Pages

| URL | Page | Description |
|-----|------|-------------|
| `/emi/` | EMI Hub | Overview with counts and quick links |
| `/emi/categories/` | Physics Categories | Table of categories with tag counts |
| `/emi/categories/create/` | Create Category | Form for new physics area |
| `/emi/tags/p/` | Physics Tags | DataTable with status and category filters |
| `/emi/tags/e/` | EvGen Tags | DataTable with status filter |
| `/emi/tags/s/` | Simu Tags | DataTable with status filter |
| `/emi/tags/r/` | Reco Tags | DataTable with status filter |
| `/emi/tags/{type}/{N}/` | Tag Detail | Parameters, description, status, linked datasets |
| `/emi/tags/{type}/create/` | Create Tag | Form with required/optional parameter fields |
| `/emi/tags/{type}/{N}/edit/` | Edit Tag | Edit draft tag parameters |
| `/emi/datasets/` | Datasets | DataTable with tag labels as links |
| `/emi/datasets/create/` | Create Dataset | Dropdowns (locked tags only), live name preview |
| `/emi/datasets/{id}/` | Dataset Detail | Tag breakdown, block management |
| `/emi/configs/` | Prod Configs | DataTable of production configs |
| `/emi/configs/create/` | Create Config | Form with grouped fieldsets |
| `/emi/configs/{id}/` | Config Detail | All settings in organized cards |
| `/emi/configs/{id}/edit/` | Edit Config | Pre-populated form |

### Navigation

EMI appears in the main nav bar as a dropdown between "State" and "PanDA/Rucio":

- EMI Hub
- Physics Tags
- EvGen Tags
- Simu Tags
- Reco Tags
- Datasets
- Prod Configs

### Tag-to-Meaning Translation

Tag labels everywhere are hyperlinks to tag detail pages. Dataset detail shows each tag with its description and key parameters inline. Dataset list shows tag labels with tooltip descriptions on hover.

## File Structure

```
src/emi/
├── __init__.py
├── apps.py              # Django app config
├── models.py            # PhysicsCategory, *Tag, Dataset, ProdConfig models
├── schemas.py           # Required/optional fields per tag type
├── forms.py             # Django forms for web UI
├── serializers.py       # DRF serializers
├── api_views.py         # REST API ViewSets
├── api_urls.py          # DRF router
├── views.py             # Web UI views + DataTable AJAX
├── urls.py              # All URL routing
├── admin.py             # Admin registration
├── migrations/
│   ├── 0001_initial.py
│   └── 0002_prodconfig.py
└── templates/emi/
    ├── emi_hub.html
    ├── physics_categories_list.html
    ├── physics_category_create.html
    ├── tag_list.html             # Generic, parameterized by tag type
    ├── tag_detail.html           # Generic
    ├── tag_create_physics.html   # Physics-specific (category selector)
    ├── tag_create.html           # Generic for e, s, r
    ├── datasets_list.html
    ├── dataset_detail.html
    ├── dataset_create.html
    ├── prod_configs_list.html
    ├── prod_config_detail.html
    └── prod_config_form.html   # Shared create/edit form
```

## Integration Points

### Modified Files

| File | Change |
|------|--------|
| `swf_monitor_project/settings.py` | `"emi"` added to INSTALLED_APPS |
| `swf_monitor_project/urls.py` | `path("emi/", include("emi.urls"))` added before monitor_app |
| `templates/base.html` | EMI dropdown added to navigation |

### Dependencies

- Uses `monitor_app.models.PersistentState` for e/s/r tag number allocation
- Uses `monitor_app.utils.DataTablesProcessor` for server-side DataTable views
- Tag list templates extend `monitor_app/_datatable_base.html`

### Future: MCP Tools

Designed for addition of MCP tools:

| Tool | Description |
|------|-------------|
| `emi_list_tags(tag_type)` | List tags with label, description, status, key params |
| `emi_get_tag(tag_label)` | Full tag detail with all parameters |
| `emi_create_tag(tag_type, ...)` | Create tag with auto-assigned number |
| `emi_list_datasets(...)` | Dataset list with tag filters |
| `emi_create_dataset(...)` | Create dataset from tag labels |
| `emi_list_physics_categories()` | Categories with counts |
