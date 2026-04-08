# TF Slices Monitoring

## Overview

TF Slices (Time Frame Slices) are sub-ranges of a FastMon sample derived from a Super Time Frame (STF) file. Each slice covers a contiguous range of Time Frames and can be processed independently by a worker in approximately 30 seconds.

## Purpose

The TF Slice system provides:
- **Parallel Processing**: Each slice is assigned to an independent worker, enabling concurrent processing
- **Bounded Work Units**: Fixed-size TF ranges make processing time predictable
- **Load Distribution**: Slices spread work across available PanDA batch workers

## Data Flow

```
StfFile (tf_count set at creation)
    └── FastMonFile  (fastmon agent samples STF → sets tf_first, tf_last, tf_count)
            └── TFSlice[]  (split of [FastMonFile.tf_first, FastMonFile.tf_last])
```

### Step 1 — STF File creation

When the DAQ simulator registers an `StfFile`, it sets `tf_count` to the total number of Time Frames contained in the file.

### Step 2 — FastMon sampling

The fastmon agent receives a notification about the new STF file and selects a contiguous sub-range of Time Frames to sample. It creates a `FastMonFile` record with:

| Field | Description |
|---|---|
| `tf_first` | Index of the first sampled TF. Must satisfy: `tf_first >= 0` and `tf_first < stf_file.tf_count` |
| `tf_last` | Index of the last sampled TF. Must satisfy: `tf_last >= tf_first` and `tf_last < stf_file.tf_count` |
| `tf_count` | Number of TFs in the sample: `tf_last - tf_first + 1` |

### Step 3 — TF Slice creation

The workflow splits the FastMon sample into a set of `TFSlice` records. Each slice covers a sub-range within the FastMon window:

- `tfslice.tf_first >= fastmon_file.tf_first`
- `tfslice.tf_last  <= fastmon_file.tf_last`
- `tfslice.tf_count = tfslice.tf_last - tfslice.tf_first + 1`

Slices are numbered sequentially via `slice_id` (1-based within the FastMon file). Together, all slices for a given `FastMonFile` cover the full `[tf_first, tf_last]` range without gaps or overlaps.

## Database Schema

### TFSlice Model

| Field | Type | Description |
|---|---|---|
| `slice_id` | Integer | Serial number within the FastMon sample (1-based) |
| `tf_first` | Integer | First TF index in this slice |
| `tf_last` | Integer | Last TF index in this slice |
| `tf_count` | Integer | Number of TFs in this slice (`tf_last - tf_first + 1`) |
| `tf_filename` | String | Filename of the parent FastMon TF file |
| `stf_filename` | String | Filename of the originating STF file |
| `run_number` | Integer | Run number |
| `status` | String | Processing status: `queued`, `processing`, `done`, `failed` |
| `retries` | Integer | Number of retry attempts |
| `assigned_worker` | String | Worker ID currently processing this slice |
| `assigned_at` | DateTime | When the worker claimed the slice |
| `completed_at` | DateTime | When processing finished |
| `metadata` | JSON | Extensible metadata |

### TF Range Constraints Summary

```
0 <= FastMonFile.tf_first < StfFile.tf_count
FastMonFile.tf_first <= TFSlice.tf_first
TFSlice.tf_last      <= FastMonFile.tf_last
                         FastMonFile.tf_last < StfFile.tf_count
```

## Web Interface

### Filtering Options
- **By Status**: Filter slices by processing state
- **By Run Number**: View all slices from a specific run
- **By STF File**: View all slices derived from a specific STF
- **By Worker**: View slices assigned to a specific worker

### Table Columns
- Slice ID
- TF Filename (parent FastMon file)
- STF Filename
- Run Number
- TF First / TF Last / TF Count
- Status
- Assigned Worker
- Created / Completed Time

## API Endpoints

### REST API
- `GET /api/tf-slices/` - List slices with filtering
- `POST /api/tf-slices/` - Create new slice(s)
- `GET /api/tf-slices/{id}/` - Get specific slice details
- `PATCH /api/tf-slices/{id}/` - Update slice status or assignment

### DataTables AJAX
- `/tf-slices/datatable/` - Server-side processing endpoint for web interface

## Monitoring Metrics

The system tracks:
- Total slices created per run
- Slice processing success/failure rate
- Average processing time per slice
- Worker utilization across slices
- Retry distribution
