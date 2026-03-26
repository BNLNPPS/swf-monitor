# PCS — Physics Configuration System

PCS manages the configuration of production tasks based on physics inputs for ePIC simulation campaigns at the Electron Ion Collider. It provides a central place to define, browse, reuse, and compose the configurations that drive Monte Carlo production.

**URL:** `/swf-monitor/pcs/`

## What It Does

Production configuration is organized as **tags** — named parameter sets that capture the settings for each stage of the simulation pipeline:

| Tag Type | Prefix | What It Captures | Example |
|----------|--------|-----------------|---------|
| **Physics** | p | Process, beam energies, species, Q2 range | p1001 — DIS NC 10x100 ep minQ2=1 |
| **EvGen** | e | Generator name and version, backgrounds | e1 — pythia8 8.310 |
| **Simulation** | s | Detector sim version, background config | s1 — npsim 26.02.0 |
| **Reconstruction** | r | Reco version, calibration, alignment | r1 — eicrecon 26.02.0 |

Physics tags are grouped by **category** (DIS, DVCS, SIDIS, EXCLUSIVE), reflected in their numbering: DIS tags are p1xxx, DVCS are p2xxx, etc.

The system is seeded with configurations from the [26.02.0 production campaign](https://eic.github.io/epic-prod/FULL/26.02.0/) — 47 physics tags, 15 evgen tags, 1 simu tag, and 1 reco tag.

## Tag Lifecycle

```
draft  ──►  locked
```

- **Draft** — editable. You can modify parameters, copy from other tags, or delete.
- **Locked** — immutable. One-way transition. Ensures reproducibility: once a tag is used in production, its meaning never changes.

Only the tag creator can edit, lock, or delete their own drafts. Anyone can copy any tag to create their own variant.

## Using the Tag Panel

The main interface is the **panel view** — a split-pane layout with a tag browser on the left and detail/edit panel on the right. Access it from the PCS dropdown in the nav bar (Physics Tags, EvGen Tags, Simu Tags, Reco Tags).

### Browsing

- **Arrow keys** navigate the tag list (keyboard focus is on the list at page load)
- **Search box** filters by any text in tag name, description, category, or parameter values
- **Status pills** filter by draft/locked
- **Creator pills** filter by who created the tag (appears when multiple creators exist)
- **Parameter dropdowns** filter by distinct values of each parameter (e.g. filter physics tags to only `beam_species=eAu`)
- **Clear** button appears when any filter is active; resets everything

### Viewing

Click any tag to see its full detail on the right: description, creator, and all parameters.

### Creating

Click **New Tag** to enter create mode. The form shows required fields (marked with *) and optional fields. For fields with known values, a dropdown offers choices; select "Other..." to enter a free value.

While creating, click any existing tag in the list — its values fill in as suggestions (grey italic). Fields you've already edited show a yellow suggestion bar instead of overwriting, so you can compose a new tag from pieces of existing ones.

The title shows the predicted next tag number (e.g. "New Tag p1020"). The actual number is assigned on save.

### Editing

Your own draft tags show an **Edit** button. Editing works in the same panel — no separate page. Changed fields appear in dark green. The Save button stays disabled until you actually change something, and re-disables if you revert all changes.

While editing, click other tags to see suggestion bars for differing values. Click "Apply" to adopt a suggested value.

### Copying

Any tag (yours or others') has a **Copy** button. This fills the create form with all values from the source tag, sets you as creator, and lets you modify before saving. Useful for creating variants of existing configurations.

### Locking and Deleting

Your own drafts show **Lock** and **Delete** buttons. Locking is permanent — confirm carefully. Deletion removes the tag.

## Tag Numbering

Tag numbers are auto-assigned. You never pick a number manually.

- **Physics tags**: `category digit * 1000 + global suffix`. The suffix increments globally across all categories, so numbers within a category may have gaps (e.g. p2001, p2005, p2020). This is expected.
- **EvGen/Simu/Reco tags**: Simple sequential increment (e1, e2, ... / s1, s2, ... / r1, r2, ...).

All counters are managed atomically via PersistentState to prevent conflicts.

## Parameter Schemas

Each tag type has required and optional parameters defined in `pcs/schemas.py`. Adding a field there makes it appear in forms and validation — no database migration needed.

**Physics (p):**
- Required: `process`, `beam_energy_electron`, `beam_energy_hadron`
- Optional: `beam_species`, `q2_range`, `decay_mode`, `hadron_charge`, `coherence`, `model`, `polarization`, `notes`

**EvGen (e):**
- Required: `generator`, `generator_version`
- Optional: `signal_freq`, `signal_status`, `bg_tag_prefix`, `bg_files`, `notes`

**Simulation (s):**
- Required: `detector_sim`, `sim_version`
- Optional: `background_config`, `digitization`, `notes`

**Reconstruction (r):**
- Required: `reco_version`, `reco_config`
- Optional: `calibration_tag`, `alignment_tag`, `notes`

## REST API

Base URL: `/swf-monitor/pcs/api/`

Tags support list, create, get, update (draft only), and lock. Replace `{type}` with `physics-tags`, `evgen-tags`, `simu-tags`, or `reco-tags`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/{type}/` | List tags |
| POST | `/{type}/` | Create tag (number auto-assigned) |
| GET | `/{type}/{number}/` | Tag detail |
| PATCH | `/{type}/{number}/` | Update draft tag |
| POST | `/{type}/{number}/lock/` | Lock tag (permanent) |

Physics tag creation requires a `category` field (digit). Tag numbers are always auto-assigned.

## Datasets

A dataset composes four locked tags with detector version information into a standardized name:

```
{scope}.{detector_version}.{detector_config}.{physics_tag}.{evgen_tag}.{simu_tag}.{reco_tag}
```

Example: `group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1`

The Rucio DID includes scope prefix and block suffix: `group.EIC:...p3001.e1.s1.r1.b1`

Block `.b1` is always present. Rucio limits datasets to 100k files; PCS manages automatic subdivision into blocks (`.b1`, `.b2`, etc.) as needed.

The **task name** is the dataset name (without the `.bN` block suffix). This is what appears in PanDA as the task identifier.

## Production Configs

A production config is a reusable template capturing everything needed to build a submit command beyond what tags and datasets define.

**Dedicated fields** (DB columns):
- **Background mixing**: Enable/disable, cross section, EvtGen file
- **Output control**: Which output files to copy (reco, full, log), Rucio usage
- **Software stack**: JUG_XL tag, container image
- **Resource targets**: Target walltime per job, events per task
- **Condor template**: HTCondor submission template text
- **PanDA overrides**: Site, queue, working group, resource type
- **Rucio overrides**: RSE, replication rules

**Submission parameters** (JSON `data` field, extensible without migrations):

| Key | Example | Purpose |
|-----|---------|---------|
| `transformation` | `runGen-00-00-02` | PanDA TRF script name/version |
| `processing_type` | `epicproduction` | PanDA classification |
| `prod_source_label` | `managed` | PanDA authorization (managed/test) |
| `vo` | `wlcg` | Virtual organization |
| `n_jobs` | `1000` | Jobs per task submission |
| `events_per_job` | `100` | Events per individual job |
| `events_per_file` | `1000` | Events per output file |
| `files_per_job` | `1` | Output files per job |
| `corecount` | `1` | Cores per job |
| `no_build` | `true` | Skip PanDA build step |
| `skip_scout` | `true` | Skip scout jobs |
| `exec_command` | `./run.sh` | Payload command (--exec) |
| `scope` | `group.EIC` | Rucio scope for submission |

Production configs are always mutable — they are working templates. The PanDA task/job spec is the immutable record of what actually ran.

## MCP Tools (TBD)

Designed for addition of MCP tools for AI-assisted tag and dataset management:

| Tool | Description |
|------|-------------|
| `pcs_list_tags(tag_type)` | List tags with label, description, status, key params |
| `pcs_get_tag(tag_label)` | Full tag detail with all parameters |
| `pcs_create_tag(tag_type, ...)` | Create tag with auto-assigned number |
| `pcs_list_datasets(...)` | Dataset list with tag filters |
| `pcs_create_dataset(...)` | Create dataset from tag labels |
