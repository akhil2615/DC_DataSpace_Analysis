# DC1 Data Space Analysis Record - Automated Fill Pipeline

Generate a space-specific DC1 Data Space Analysis Record (`.docx`) from live Salesforce Data Cloud metadata.

This repository is focused on the DC1 automated fill pipeline only. It does not require MCP server setup.

## Table of Contents

- Overview
- Prerequisites
- Access and Permissions Checklist
- Local Setup (Step by Step)
- Authentication and Org Context
- Data Space Selection
- How to Run
- Output Files and What They Mean
- Recommended Run Patterns
- Troubleshooting
- Security and Operational Notes
- Repository Structure

## Overview

The pipeline has two main stages:

1. Fetch metadata from Data Cloud APIs into a local cache (`.dc1-cache/`).
2. Fill the DC1 Word template for a specific data space using cached metadata.

Core behavior:

- Space-specific output (`--space <name>`)
- Retry and exponential backoff for transient failures
- Adaptive throttling when API usage is high
- Provenance-aware output and run logs for auditability

## Prerequisites

Install and verify the following before your first run.

### 1) Operating System

- Windows 10/11, macOS, or Linux
- Shell examples below are shown with generic commands and work in PowerShell with equivalent syntax

### 2) Python

- Python 3.10 or newer
- Verify:

```bash
python --version
```

If your system uses `python3`, run:

```bash
python3 --version
```

### 3) Salesforce CLI

- Salesforce CLI (`sf`) installed and available in PATH
- Verify:

```bash
sf --version
```

### 4) Git (for cloning and updates)

- Verify:

```bash
git --version
```

### 5) DC1 template file

- The default template is included in this repository root as `DC1_Data_Space_Analysis_Record_TEMPLATE.docx`.
- If your team uses a custom template filename/path, align script expectations before running.

## Access and Permissions Checklist

The logged-in Salesforce user should have:

- Access to the target Salesforce org
- Data Cloud enabled in that org
- Permission to read Data Cloud metadata required by this pipeline
- Access to the target data space(s)
- API access enabled

Recommended role profile:

- Data Cloud admin or architecture-level read access
- Ability to inspect streams, DLOs, DMOs, segments, calculated insights, activations, and identity resolution metadata

If access is missing, the fetch step may return 400/403/404/500 for specific endpoints.

## Local Setup (Step by Step)

### 1) Clone repository

```bash
git clone https://github.com/akhil2615/DC_DataSpace_Analysis.git
cd DC_DataSpace_Analysis
```

### 2) Create and activate virtual environment

Windows (PowerShell):

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3) Install Python dependencies

If `requirements.txt` is present:

```bash
pip install -r requirements.txt
```

If you need to install manually, ensure at least:

```bash
pip install python-docx
```

### 4) Confirm scripts are discoverable

```bash
python -u scripts/dc1_fetch_live.py --help
python -u scripts/fill_dc1_live.py --help
python -u scripts/dc1_run.py --help
```

## Authentication and Org Context

The pipeline uses your active Salesforce CLI login context.

### 1) Login (if not already logged in)

```bash
sf org login web --alias my-org
```

### 2) Verify current org context

```bash
sf org display --json
```

Validate:

- `orgId` is the org you want
- Instance URLs are expected
- Connected username is correct

### 3) Switch target org (if needed)

```bash
sf config set target-org my-org
sf org display --json
```

Important: Always verify org context before running fetch.

## Data Space Selection

This pipeline is designed for data-space-specific output.

- Generate one document for one space:
  - `--space <DATA_SPACE_NAME>`
- Optionally generate all spaces available in cache:
  - `--all-spaces`

For production usage, prefer one space per run for better control and review.

## How to Run

## A) First run in a new org (recommended)

Step 1: Fresh metadata fetch

```bash
python -u scripts/dc1_fetch_live.py --clean-cache --workers 8 --max-retries 4 --retry-base-ms 400 --adaptive-throttle
```

What this does:

- Clears stale cache (`--clean-cache`)
- Fetches metadata used by the DC1 template
- Retries transient failures
- Slows automatically when API usage is high

Step 2: Generate one data space record

```bash
python -u scripts/fill_dc1_live.py --space default
```

Replace `default` with your target data space name.

## B) Repeat run with existing cache

If metadata has not changed significantly and you only want to regenerate doc output:

```bash
python -u scripts/fill_dc1_live.py --space <DATA_SPACE_NAME>
```

## C) One-shot orchestrated run (fetch + fill + reports)

```bash
python -u scripts/dc1_run.py --clean-cache --spaces default --output-dir runs/latest/docs
```

This is useful for shareable run artifacts and audit trails.

## D) Optional all-spaces output from current cache

```bash
python -u scripts/fill_dc1_live.py --all-spaces --output-dir runs/latest/docs
```

## Output Files and What They Mean

### Generated DC1 document

Example pattern:

- `DC1_Data_Space_Analysis_Record_<space>_LIVE_<orgId>_<yyyymmdd>.docx`

This is the primary artifact you share with architects/stakeholders.

### Cache directory

- `.dc1-cache/`

Contains raw fetched metadata JSON snapshots used to populate the template.

### Orchestrator run artifacts

When using `dc1_run.py`, you also get:

- `runs/<timestamp>/manifest.json` - machine-readable run metadata
- `runs/<timestamp>/report.md` - human-readable run summary
- `runs/<timestamp>/report.csv` - tabular run summary
- `runs/<timestamp>/*.log` - execution logs

## Recommended Run Patterns

### Pattern 1: Controlled single-space production run

1. Verify org context
2. Fresh fetch with throttling
3. Generate one space document
4. Review appendices (human-input and needs-review items)
5. Share output `.docx`

### Pattern 2: Fast regen after minor edits

1. Keep existing cache
2. Re-run fill for a single space
3. Re-review changed output sections

### Pattern 3: Org-level baseline refresh

1. Run orchestrator with `--clean-cache`
2. Generate space outputs needed for the cycle
3. Archive run folder for audit evidence

## Troubleshooting

### `sf: command not found` or `sf` not recognized

- Install Salesforce CLI
- Restart terminal
- Re-run `sf --version`

### Wrong org data in output

- Run `sf org display --json`
- Set correct target org:
  - `sf config set target-org <alias>`
- Re-run fetch with `--clean-cache`

### No output for expected data space

- Confirm exact data space name
- Check whether metadata exists for that space
- Regenerate after fresh fetch

### 403/permission errors

- Validate user permissions for Data Cloud metadata endpoints
- Confirm your user has access to the data space and Data Cloud objects

### 429/rate-limit behavior

- Keep `--adaptive-throttle` enabled
- Reduce `--workers` (for example from `8` to `4`)
- Retry after short wait

### Partial or sparse sections in document

- Some columns are intentionally manual/human-review fields
- Use the pipeline appendices and `DC1_RECORD_PIPELINE.md` guidance to complete manual parts

### Template/path issues

- Ensure the expected DC1 template file exists at configured location
- Avoid renaming template without updating script expectations

## Security and Operational Notes

- Do not commit org-sensitive cache or run outputs unless explicitly required by your process.
- Keep `.dc1-cache/` and `runs/` ignored in git.
- Prefer least-privilege org users with required read access.
- Review generated documents before distribution (four-eyes review is recommended).

## Repository Structure

- `DC1_RECORD_PIPELINE.md` - detailed pipeline design and section mapping
- `scripts/dc1_fetch_live.py` - optimized fetcher
- `scripts/fill_dc1_live.py` - DC1 template filler
- `scripts/dc1_run.py` - orchestrator with reports
- `scripts/dc1_cache_summary.py` - cache summary helper
- `scripts/dc1_audit.py` - output document audit helper

## Additional Documentation

For full technical details, section-by-section fill behavior, endpoint notes, timing guidance, and manual intervention matrix, see:

- `DC1_RECORD_PIPELINE.md`
