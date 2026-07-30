# Data Cloud Data Space Analysis - Automated Fill Pipeline

Generate a space-specific Data Cloud Data Space Analysis record (`.docx`) from live Salesforce Data Cloud metadata.

This repository is focused on the automated Data Cloud Data Space Analysis pipeline. It does not require MCP server setup.

## Start Here (First-Time Setup)

Use this exact flow for first-time setup. This is the primary onboarding path.

### Step 0: Clone the repository

Windows / macOS / Linux:

```bash
git clone https://github.com/akhil2615/DC_DataSpace_Analysis.git
cd DC_DataSpace_Analysis
```

### Step 1: Run setup + start launcher

Windows (PowerShell):

```bash
powershell -ExecutionPolicy Bypass -File scripts/setup_windows.ps1
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

macOS / Linux:

```bash
chmod +x scripts/setup_unix.sh
./scripts/setup_unix.sh
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

If setup says no active Salesforce login, run:

```bash
sf org login web --alias my-org
sf config set target-org my-org --global
sf org display --json
```

Then start launcher again with the same uvicorn command.

Then open:

- [http://127.0.0.1:8000](http://127.0.0.1:8000)

### Step 2: First run in the UI

1. Click **Check Setup**
2. Click **Load Latest Data Spaces**
3. Select a data space
4. Click **Generate Analysis Files**
5. Download both files:
   - Analysis document (`.docx`)
   - Metadata workbook (`.xlsx`)

If **Check Setup** shows `Setup Ready (Data not loaded)`, that is normal for first run. Click **Load Latest Data Spaces**.

### One-command update for existing users

From the cloned repo folder:

```bash
git pull
```

## Table of Contents

- Overview
- Prerequisites
- Onboarding Expectations
- Access and Permissions Checklist
- Local Setup (Step by Step)
- Authentication and Org Context
- Why This Uses CLI User Context (Not MCP)
- Data Space Selection
- How to Run
- Local Web Launcher (Dropdown UI)
- Output Files and What They Mean
- Recommended Run Patterns
- Troubleshooting
- Security and Operational Notes
- Repository Structure

## Overview

The pipeline has two main stages:

1. Fetch metadata from Data Cloud APIs into a local cache (`.data-space-analysis-cache/`).
2. Fill the analysis Word template for a specific data space using cached metadata.

Core behavior:

- Space-specific output (`--space <name>`)
- Retry and exponential backoff for transient failures
- Adaptive throttling when API usage is high
- Provenance-aware output and run logs for auditability

## Prerequisites

If you are setting this up for the first time, complete **Start Here (First-Time Setup)** first, then use this section as a detailed checklist.

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

### 5) Analysis template file

- The default template is included in this repository root as `DataCloud_DataSpace_Analysis_Record_TEMPLATE.docx`.
- If your team uses a custom template filename/path, align script expectations before running.

## Onboarding Expectations

Short answer: users still need some local prerequisites. The web page simplifies usage, but it does not remove machine setup.

Use **Start Here (First-Time Setup)** for the fastest path (Git clone + setup commands). This section explains the reasoning and boundaries.

What the web launcher handles:

- guided run steps for setup checks, data space loading, and analysis generation
- one-click execution of data retrieval + document/workbook creation
- run status, logs, and file downloads

What must still exist on each user machine:

- Python 3.10+
- Salesforce CLI (`sf`)
- a valid Salesforce CLI login session for the target org
- Windows or macOS terminal access (PowerShell, Terminal, or equivalent)

Platform note:

- Supported for setup scripts: **Windows** and **macOS/Linux**.
- **iOS (iPhone/iPad)** is not supported for local execution because Salesforce CLI and local Python automation are required.

Why this is required:

- the launcher runs local Python scripts for metadata retrieval and file generation
- Data Cloud metadata retrieval uses authenticated Salesforce CLI user context

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

This section is detailed reference. For most users, use **Start Here (First-Time Setup)** above.

### Fastest onboarding (recommended scripts)

Windows PowerShell:

```bash
powershell -ExecutionPolicy Bypass -File scripts/setup_windows.ps1
```

macOS/Linux:

```bash
chmod +x scripts/setup_unix.sh
./scripts/setup_unix.sh
```

These scripts:

- validate required tools (`python`, `git`, `sf`)
- create `.venv` if missing
- install `requirements.txt`
- check Salesforce CLI org auth context
- print the exact launcher start command

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

For the local web launcher, install:

```bash
pip install -r requirements.txt
```

### 4) Confirm scripts are discoverable

```bash
python -u scripts/fetch_live.py --help
python -u scripts/fill_live.py --help
python -u scripts/run_pipeline.py --help
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
sf config set target-org my-org --global
sf org display --json
```

Important: Always verify org context before running fetch.

## Why This Uses CLI User Context (Not MCP)

This project intentionally uses Salesforce CLI user authentication (`sf org login`) instead of an MCP integration layer.

### Design decision summary

- Primary goal: produce a portable automation pipeline any architect can run from a terminal with minimal dependencies.
- Execution model: direct API calls from Python scripts using the active authenticated Salesforce user context.
- Authentication source: CLI-managed access token and org context.

### Why this is the preferred approach for this repository

1. Operational simplicity
- No MCP server installation, MCP config, or tool routing is required.
- Fewer moving parts means less setup overhead for new users.
- Easier to onboard architecture and delivery teams that already use `sf`.

2. Environment portability
- Works in local dev environments, jump boxes, or CI runners where Python + `sf` are available.
- Does not depend on IDE-specific runtime behavior.
- Keeps the pipeline runnable outside Cursor workflows.

3. Predictable auth and access control
- Uses the same identity and permissions model users already manage in Salesforce CLI.
- Access scope is naturally governed by the logged-in user profile/permission sets.
- Reduces ambiguity about which credentials are being used at runtime.

4. Easier audit and supportability
- Org context can be verified with one command: `sf org display --json`.
- Troubleshooting auth and target-org issues is standardized across Salesforce teams.
- Run logs and artifacts directly map to CLI session context and org metadata responses.

5. Better fit for document-generation use case
- This pipeline performs deterministic metadata extraction + document fill.
- It does not require tool orchestration or interactive assistant workflows to produce outputs.
- Direct script execution is faster to operationalize for repeatable production runs.

### Security and governance implications

- The pipeline never requires storing long-lived credentials inside the repository.
- Token lifecycle and login flows are managed by Salesforce CLI standards.
- Access is least-privilege by default if the logged-in user is scoped appropriately.

### When MCP can still be useful (outside this repo)

MCP can be valuable for interactive exploration, ad-hoc discovery, and assistant-driven workflows.
For this repository's objective (repeatable analysis output generation), CLI user-context execution is intentionally the baseline.

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
python -u scripts/fetch_live.py --clean-cache --workers 8 --max-retries 4 --retry-base-ms 400 --adaptive-throttle
```

What this does:

- Clears stale cache (`--clean-cache`)
- Fetches metadata used by the analysis template
- Retries transient failures
- Slows automatically when API usage is high

Step 2: Generate one data space record

```bash
python -u scripts/fill_live.py --space default
```

Replace `default` with your target data space name.

## B) Repeat run with existing cache

If metadata has not changed significantly and you only want to regenerate doc output:

```bash
python -u scripts/fill_live.py --space <DATA_SPACE_NAME>
```

## C) One-shot orchestrated run (fetch + fill + reports)

```bash
python -u scripts/run_pipeline.py --clean-cache --spaces default --output-dir runs/latest/docs
```

This is useful for shareable run artifacts and audit trails.

## D) Optional all-spaces output from current cache

```bash
python -u scripts/fill_live.py --all-spaces --output-dir runs/latest/docs
```

## Output Files and What They Mean

### Generated analysis document

Example pattern:

- `DataCloud_DataSpace_Analysis_Record_<space>_LIVE_<orgId>_<yyyymmdd>.docx`

This is the primary artifact you share with architects/stakeholders.

### Cache directory

- `.data-space-analysis-cache/`

Contains raw fetched metadata JSON snapshots used to populate the template.

### Orchestrator run artifacts

When using `run_pipeline.py`, you also get:

- `runs/<timestamp>/manifest.json` - machine-readable run metadata
- `runs/<timestamp>/report.md` - human-readable run summary
- `runs/<timestamp>/report.csv` - tabular run summary
- `runs/<timestamp>/*.log` - execution logs

## Local Web Launcher (Dropdown UI)

The repository now includes a lightweight local web app so users can select a data space from a dropdown and run the pipeline without typing full commands.

### Start the web app

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

Open:

- [http://127.0.0.1:8000](http://127.0.0.1:8000)

### UI workflow

1. Click **Check Setup** to validate login, template, and required files.
2. Click **Load Latest Data Spaces** to refresh the data space dropdown from your org.
3. Select the target data space.
4. Keep **Pull latest metadata before generating document** enabled for current-state output (recommended).
5. Click **Generate Analysis Files**.
6. Watch live logs and download both outputs when complete:
   - analysis document (`.docx`)
   - Metadata workbook (`.xlsx`) with one summary tab and JSON-driven tabs

Tip: use the **Start Here (First-Time Setup)** section in this README for new-user onboarding before opening the launcher.

### Web-run artifacts

- UI-driven runs are written under `runs/web/<run-id>/`.
- Document output is written under `runs/web/<run-id>/docs/`.
- Document file pattern (web launcher output):
  - `DataCloud_DataSpace_Analysis_Record_<space>_<run-id>.docx`
- Workbook bundle pattern:
  - `DataCloud_DataSpace_Metadata_Bundle_<space>_<run-id>.xlsx`
- Workbook contents:
  - `Summary` tab with run metadata and cache file index
  - one or more tabs per cache file (records and raw JSON views)

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
  - `sf config set target-org <alias> --global`
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
- Use the pipeline appendices and `DATA_SPACE_ANALYSIS_PIPELINE.md` guidance to complete manual parts

### Template/path issues

- Ensure the expected analysis template file exists at configured location
- Avoid renaming template without updating script expectations

## Security and Operational Notes

- Do not commit org-sensitive cache or run outputs unless explicitly required by your process.
- Keep `.data-space-analysis-cache/` and `runs/` ignored in git.
- Prefer least-privilege org users with required read access.
- Review generated documents before distribution (four-eyes review is recommended).

## Repository Structure

- `DATA_SPACE_ANALYSIS_PIPELINE.md` - detailed pipeline design and section mapping
- `scripts/fetch_live.py` - optimized fetcher entrypoint
- `scripts/fill_live.py` - analysis template fill entrypoint
- `scripts/run_pipeline.py` - orchestrator entrypoint with reports
- `scripts/cache_summary.py` - cache summary helper
- `scripts/audit_output.py` - output document audit helper

## Additional Documentation

For full technical details, section-by-section fill behavior, endpoint notes, timing guidance, and manual intervention matrix, see:

- `DATA_SPACE_ANALYSIS_PIPELINE.md`
