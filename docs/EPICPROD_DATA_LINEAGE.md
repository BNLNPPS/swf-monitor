# ePIC Production Data Lineage

A production system must record and reference the data it produces. epicprod
produces reconstruction datasets that land in Rucio; PCS already holds their full
production provenance — physics, EvGen, simulation, reconstruction tags,
campaign, requestor, use flags, status — with rich filtering over all of it. What
it does not yet hold is the **explicit Rucio reference** for each produced
dataset: the `epic:/RECO/…` DID(s), their RSEs, and file counts. Recording those
closes the production record — and, the provenance already being filterable, lets
the catalog locate produced data by any provenance dimension.

The immediate application is verifying campaign **lineage and completeness** —
the record the production team keeps today by hand (the `default_datasets`
catalogue and monthly completion email).

## Problem and scope

The requested-side information is already in the PCS task database —
`default_datasets` seeded our catalog and everything it shows is in our
`ProdTask` records. What is missing, in our catalog *and* on the
`default_datasets` HTML page, is the explicit link to Rucio. Without it a filter
cannot yet resolve to Rucio datasets or files; with it, it can.

The work is to **gather** the Rucio links and write them onto the existing task
records' extensible JSON — not a second database of mappings. Stored there, the
links unlock two capabilities over the catalog's existing filters:

- **Reference** — resolve a filtered task set to its produced Rucio
  datasets/files. A plain catalog read, no credential.
- **Access** — fetch the produced data itself over XRootD from those links:
  sample a file, pull a whole file when small (as the payload-log doer does for
  logs), examine it. The agent's credentialed xrootd substrate; no credential in
  the web tier.

So we win not just the correlation but **data access**.

Gathering is **transitional, and specific to pre-PanDA data**. The current
(Condor-based) production system does not make or preserve the production→Rucio
connection in flight, which is exactly why it must be reconstructed after the
fact — and why the production team's monthly completion emails exist. PanDA
records that connection in flight, so PanDA-produced data carries it from the
start. The sweep therefore backfills pre-PanDA campaigns only; reference and
access over the recorded links are permanent.

## Derivation (requested path → DID → replicas)

The procedure that recovers a produced dataset's Rucio reference from its
requested path.

**Lineage — requested path → DID:**

- Requested path: read from the catalog at `ProdTask.input_source_location`
  (the `Dataset.source_location` property, i.e.
  `Dataset.metadata['source']['location']`; `csv_file` is the fallback), e.g.
  `/volatile/eic/EPIC/EVGEN/DDIS/rapgap3.310-1.0/noRad/ep/10x100`. Seeded from the
  `default_datasets` `Dataset Path` column.
- Strip the `/volatile/eic/EPIC/EVGEN/` prefix to a `suffix`, then glob Rucio for
  `epic:/RECO*<campaign>*/<suffix>`. The `*` absorbs
  `/<campaign>/<detector_config>` (e.g. `/26.04.1/epic_craterlake`). Any hit
  beginning `epic:/RECO/` ⇒ available, plus the matched DID(s).
- DID grammar: `epic:/RECO/<campaign>/<detector_config>/<suffix>`.
- Special case: `EXCLUSIVE/UPSILON_ABCONV` matched at *file* level — container
  glob → `rucio list-content` → basename match on `*.eicrecon.edm4eic.root`.

**Completeness — DID → replicas, counts:**

- `rucio list-rules <did>` → keep RSEs in **{BNL-XRD, EIC-XRD}** (drop
  JLAB-TAPE-SE).
- `rucio list-content <did>` → **file count**; per-RSE XRootD prefixes
  `root://epicxrd1.sdcc.bnl.gov:1095//eic/EPIC/` (BNL-XRD) and
  `root://dtn-eic.jlab.org//volatile/eic/EPIC` (EIC-XRD).
- **Expected vs. actual** — compare the file count against the files expected
  from the request's `nevents` to flag partial campaigns.

The doer invokes these through the **Rucio CLI** (which the Rucio team implement
over their REST API) under the agent's long-proxy rather than hand-rolling REST —
the standardize-on-CLIs choice we also apply to the panda CLI and to our own
prod-navigation CLI. Prerequisite: confirm which `rucio` binary the agent can
call under its proxy (the CVMFS distribution).

## PCS data model (write target)

The links go onto **`ProdTask.overrides`** (JSONField) under a reserved key — the
same interim convention that already holds `input_dataset_dids` and the
`public_catalog_*` fields. Relevant `ProdTask` fields:

- `input_source_location` (property; `Dataset.source_location`, i.e.
  `Dataset.metadata['source']['location']`, `csv_file` fallback) — the requested
  `/volatile/eic/EPIC/EVGEN/<suffix>` path the glob derives from.
- `campaign` → `Campaign` (FK) — supplies the `<campaign>` glob segment.
- `dataset` → `Dataset` (FK) — the PCS *output* dataset. Its `did` is the
  PCS-composed `group.EIC:….b{N}` identifier and `detector_config` the detector;
  a **different namespace** from the produced `epic:/RECO/…` Rucio DID.
- `request` → `ProdRequest` (FK) — originating PWG/DSC request; supplies
  `nevents` for the expected-vs-actual check.
- `overrides` — the interim JSON the links are written to.

`overrides` shape — one `rucio_output` object per produced DID, suffixed
`rucio_output_2`, `_3`, … when a task resolves to more than one; file count
recorded per dataset:

```json
{
  "rucio_output": {
    "did": "epic:/RECO/26.04.1/epic_craterlake/<suffix>",
    "rses": ["BNL-XRD", "EIC-XRD"],
    "file_count": 1234,
    "expected_files": 1280,
    "checked_at": "<iso8601>"
  }
}
```

## Architecture

**Gather** — standard credentialed-async pattern, no new substrate:
`_handle_campaign_provenance_sweep` (validate inline) → `run_in_background` doer
(holds the proxy; derives the glob, invokes the Rucio CLI, writes each task's
`overrides`) → completion event on `/topic/epictopic` → the catalog updates live
via `EventSource`, internally and through the swf-remote streaming proxy. The web
tier holds no credential.

- Trigger: a per-campaign **Sweep** button (on demand) and a **nightly** cron
  firing the same handler (replaces the monthly email).
- Unit of work: one campaign — iterate its `ProdTask` rows, fan the per-task
  Rucio checks across the worker pool; the receiver thread never blocks.

**Reference** — a catalog read over the stored links, no credential: the existing
filter set (`EPICPROD_TASK_CATALOG.md` §7) collects the tasks' `rucio_output`
DIDs across the filtered set. Dataset-level reference is a pure read of the cached
links; file/PFN expansion is resolved **live against Rucio on demand** — file
lists are not cached, Rucio is their authority. Surfaced in the catalog's "Rucio
Monitor" feed (DID link, RSE badges, file count) per `EPICPROD_TASK_CATALOG.md`
§6, and as an action to list or export the produced datasets for a selection. A
**prod-navigation CLI** is a second front-end on this same filter→resolve REST,
covered by its own design doc (downstream of these links existing).

**Access** — fetch the produced data over XRootD, the same credentialed substrate
as payload-log: a `run_in_background` doer constructs the per-RSE XRootD PFN
(replace the `epic:` DID prefix with the RSE prefix), `xrdcp`s a sample or a small
whole file under the proxy, caches it, and pushes the result to the browser on
`/topic/epictopic`. Sample and small-whole-file pull only; inspection follows the
payload-log model. Each is an individual handler.

## Proto-plan

**Phase 1 — gather.** Iterate a campaign's `ProdTask` rows; derive the RECO DID
glob from the requested path + `campaign` + `detector_config`; resolve DID(s),
RSEs, file counts, and expected-vs-actual; write `overrides.rucio_output`. Render
in the catalog; push on completion. Validate by spot-checking a sample against an
independent manual Rucio query.

**Phase 2 — reference.** Catalog filter → the produced Rucio datasets, with
on-demand file/PFN expansion (live, uncached) — the system surfacing the data it
produced from its provenance record. The prod-navigation CLI is a second
front-end on this REST.

**Phase 3 — access.** Fetch produced data over XRootD from the stored links —
sample, small-whole-file pull, examine — reusing the payload-log xrootd doer.

**Phase 4 — capture at source (PanDA data).** PanDA makes the production→Rucio
connection in flight, so for PanDA-produced tasks the output DID is recorded at
submission time (extending `record-submission`, which already writes
`panda_task_id`) rather than reconstructed by a sweep. The sweep stays a backfill
tool for pre-PanDA campaigns.
