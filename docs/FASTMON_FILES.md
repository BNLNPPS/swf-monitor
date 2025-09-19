# FastMon Files (Time Frames) Monitoring

## Overview

FastMon Files represent Time Frame (TF) files that are subsampled from Super Time Frame (STF) files for rapid monitoring and quality assessment. These smaller data samples enable real-time monitoring of data quality without processing entire STF files.

## Purpose

The FastMon system provides:
- **Rapid Quality Assessment**: Quick validation of detector data through sampled Time Frames
- **Real-time Monitoring**: Immediate feedback on data collection quality
- **Efficient Processing**: Analyze representative samples without full STF processing overhead
- **Early Problem Detection**: Identify issues during data taking rather than post-run

## Data Flow

1. **STF Generation**: DAQ simulator creates Super Time Frame files during physics data taking
2. **TF Sampling**: FastMon agent samples Time Frames from each STF (typically 7 TFs per STF at 15% size fraction)
3. **Registration**: TF files are registered in the FastMonFile database table
4. **Broadcasting**: TF availability is broadcast via ActiveMQ for monitoring clients
5. **Analysis**: Fast monitoring clients process TFs for quality metrics

## Database Schema

### FastMonFile Model
- `tf_file_id`: Unique identifier for the Time Frame file
- `stf_file`: Reference to parent STF file
- `tf_filename`: Unique filename following pattern: `{stf_base}_tf_{sequence}.tf`
- `file_size_bytes`: Size of the TF file (typically ~15% of STF size)
- `status`: Processing status (registered, processing, processed, failed, done)
- `metadata`: JSON field containing:
  - Simulation parameters
  - Creation timestamp
  - Agent information
  - Machine state and substate
  - Timing information

## Web Interface

The FastMon Files view provides:

### Filtering Options
- **By Status**: Filter TFs by processing status
- **By Parent STF**: View all TFs from a specific STF file
- **By Run Number**: View all TFs from a specific run

### Table Columns
- TF Filename
- Parent STF File (clickable to STF detail page)
- Run Number (clickable to run detail page)
- File Size
- Status
- Creation Time

### Features
- Server-side pagination for large datasets
- Global search across filenames and run numbers
- Quick filter links for easy navigation
- Export capabilities for offline analysis

## API Endpoints

### REST API
- `GET /api/fastmon-files/` - List all TF files with filtering
- `POST /api/fastmon-files/` - Register new TF file
- `GET /api/fastmon-files/{id}/` - Get specific TF file details
- `PATCH /api/fastmon-files/{id}/` - Update TF file status

### DataTables AJAX
- `/fastmon-files/datatable/` - Server-side processing endpoint for web interface

## Monitoring Metrics

The system tracks:
- Total TF files generated per run
- TF processing success rate
- Average TF file size
- Time from STF creation to TF availability
- Distribution of TFs across machine states