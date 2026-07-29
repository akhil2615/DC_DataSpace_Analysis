"""Production runner for DC1 generation.

Runs one fetch on the current CLI user context, then generates one document per
data space from cache, with a run manifest for auditability.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
RUNS = REPO / "runs"


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True)


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def parse_saved_path(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.strip().startswith("Saved:"):
            return line.split("Saved:", 1)[1].strip()
    return ""


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Fetch once and generate DC1 docs per data space.")
    ap.add_argument("--all-connectors", action="store_true", help="Probe all connector types during fetch.")
    ap.add_argument("--workers", type=int, default=8, help="Fetch concurrency.")
    ap.add_argument("--max-retries", type=int, default=4, help="Retry count for transient API failures.")
    ap.add_argument("--retry-base-ms", type=int, default=400, help="Backoff base in milliseconds.")
    ap.add_argument("--clean-cache", action="store_true", help="Clear cache before fetch.")
    ap.add_argument(
        "--adaptive-throttle",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable adaptive API-limit throttling during fetch.",
    )
    ap.add_argument("--spaces", nargs="*", help="Explicit data space names. Default: all from cache.")
    ap.add_argument("--output-dir", default="runs/latest/docs", help="Output directory for generated docs.")
    args = ap.parse_args()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    docs_dir = (REPO / args.output_dir).resolve()
    docs_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "startedAtUtc": ts,
        "cwd": str(REPO),
        "docsDir": str(docs_dir),
        "fetch": {},
        "documents": [],
    }

    fetch_cmd = [
        sys.executable,
        str(SCRIPTS / "dc1_fetch_live.py"),
        "--workers",
        str(args.workers),
        "--max-retries",
        str(args.max_retries),
        "--retry-base-ms",
        str(args.retry_base_ms),
    ]
    if args.all_connectors:
        fetch_cmd.append("--all-connectors")
    if args.clean_cache:
        fetch_cmd.append("--clean-cache")
    if args.adaptive_throttle:
        fetch_cmd.append("--adaptive-throttle")
    else:
        fetch_cmd.append("--no-adaptive-throttle")
    print("running fetch:", " ".join(fetch_cmd))
    t_fetch = time.time()
    fetch = run(fetch_cmd, REPO)
    fetch_elapsed = round(time.time() - t_fetch, 3)
    (run_dir / "fetch.stdout.log").write_text(fetch.stdout, encoding="utf-8")
    (run_dir / "fetch.stderr.log").write_text(fetch.stderr, encoding="utf-8")
    manifest["fetch"]["exitCode"] = fetch.returncode
    manifest["fetch"]["elapsedSeconds"] = fetch_elapsed
    if fetch.returncode != 0:
        manifest["status"] = "failed"
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(fetch.stdout)
        print(fetch.stderr, file=sys.stderr)
        raise SystemExit(fetch.returncode)

    prov = load_json(REPO / ".dc1-cache" / "_provenance.json")
    spaces = args.spaces or [s.get("name") for s in load_json(REPO / ".dc1-cache" / "data-spaces.json").get("records", []) if s.get("name")]
    if not spaces:
        raise SystemExit("No data spaces found in cache.")

    for sp in spaces:
        fill_cmd = [
            sys.executable,
            str(SCRIPTS / "fill_dc1_live.py"),
            "--space",
            sp,
            "--output-dir",
            str(docs_dir),
        ]
        print("running fill:", " ".join(fill_cmd))
        t_fill = time.time()
        res = run(fill_cmd, REPO)
        fill_elapsed = round(time.time() - t_fill, 3)
        (run_dir / f"fill_{sp}.stdout.log").write_text(res.stdout, encoding="utf-8")
        (run_dir / f"fill_{sp}.stderr.log").write_text(res.stderr, encoding="utf-8")
        manifest["documents"].append(
            {
                "space": sp,
                "exitCode": res.returncode,
                "elapsedSeconds": fill_elapsed,
                "outputDocx": parse_saved_path(res.stdout),
            }
        )

    manifest["status"] = "ok" if all(d["exitCode"] == 0 for d in manifest["documents"]) else "partial_failure"
    manifest["finishedAtUtc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest["provenance"] = prov
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Architect-friendly report artifacts.
    md = [
        "# DC1 Batch Run Report",
        "",
        f"- **Run start (UTC):** {manifest['startedAtUtc']}",
        f"- **Run end (UTC):** {manifest['finishedAtUtc']}",
        f"- **Status:** {manifest['status']}",
        f"- **Org ID:** {prov.get('orgId', '')}",
        f"- **Instance:** {prov.get('instanceUrl', '')}",
        f"- **Fetch elapsed:** {manifest['fetch'].get('elapsedSeconds', '?')} s",
        f"- **Fetch API calls:** {prov.get('apiCalls', '?')}",
        f"- **Fetch retries:** {prov.get('retries', '?')}",
        f"- **Max API usage seen:** {prov.get('maxApiUsagePercent', '?')}%",
        f"- **Adaptive throttle sleep:** {prov.get('adaptiveThrottleSleepSeconds', '?')} s",
        "",
        "## Per-space documents",
        "",
        "| Space | Exit | Fill Seconds | Output |",
        "|---|---:|---:|---|",
    ]
    for d in manifest["documents"]:
        md.append(
            f"| {d['space']} | {d['exitCode']} | {d.get('elapsedSeconds', '')} | {d.get('outputDocx', '')} |"
        )
    (run_dir / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    with (run_dir / "report.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["space", "exitCode", "elapsedSeconds", "outputDocx"],
        )
        w.writeheader()
        for d in manifest["documents"]:
            w.writerow(
                {
                    "space": d["space"],
                    "exitCode": d["exitCode"],
                    "elapsedSeconds": d.get("elapsedSeconds", ""),
                    "outputDocx": d.get("outputDocx", ""),
                }
            )
    print(f"Run complete. Manifest: {run_dir / 'manifest.json'}")


if __name__ == "__main__":
    main()

