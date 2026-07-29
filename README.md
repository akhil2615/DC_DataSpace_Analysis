# DC1 Data Space Analysis Record — Automated Fill Pipeline

This repository contains a production-focused pipeline to generate the **DC1 Data Space Analysis Record** from live Salesforce Data Cloud metadata.

It is **not** an MCP server setup guide. It runs directly with the logged-in Salesforce CLI user context.

## What this repo does

- Fetches required Data Cloud metadata into a local cache (`.dc1-cache/`)
- Generates a **space-specific** DC1 `.docx` output (`--space <name>`)
- Preserves provenance and adds review-focused appendices
- Supports retries, adaptive API-limit throttling, and run manifests/reports

## Quickstart (single data space)

```bash
# 1) Confirm org context
sf org display --json

# 2) Fresh fetch
python -u scripts/dc1_fetch_live.py --clean-cache --workers 8 --max-retries 4 --retry-base-ms 400 --adaptive-throttle

# 3) Generate one document
python -u scripts/fill_dc1_live.py --space default
```

## Key commands

- Single data space:
  - `python -u scripts/fill_dc1_live.py --space <DATA_SPACE_NAME>`
- All cached spaces:
  - `python -u scripts/fill_dc1_live.py --all-spaces --output-dir runs/latest/docs`
- Orchestrated run with report artifacts:
  - `python -u scripts/dc1_run.py --clean-cache --spaces default --output-dir runs/latest/docs`

## Output artifacts

- Generated document(s):
  - `DC1_Data_Space_Analysis_Record_<space>_LIVE_<orgId>_<yyyymmdd>.docx`
- Run artifacts (orchestrator):
  - `runs/<timestamp>/manifest.json`
  - `runs/<timestamp>/report.md`
  - `runs/<timestamp>/report.csv`
  - `runs/<timestamp>/*.log`

## Repository contents

- `DC1_RECORD_PIPELINE.md` — full technical guide (sections filled, endpoint behavior, timing, troubleshooting)
- `scripts/dc1_fetch_live.py` — optimized fetcher (retry/backoff/adaptive throttle)
- `scripts/fill_dc1_live.py` — DC1 document generator (`--space`, `--all-spaces`)
- `scripts/dc1_run.py` — fetch + fill orchestrator with report generation
- `scripts/dc1_cache_summary.py` — cache status summary
- `scripts/dc1_audit.py` — output doc audit utility

## Notes

- The pipeline uses the active Salesforce CLI login; ensure the correct org is set before running.
- Keep `.dc1-cache/` and `runs/` out of commits (already in `.gitignore`).
- For full operational guidance, read `DC1_RECORD_PIPELINE.md`.
