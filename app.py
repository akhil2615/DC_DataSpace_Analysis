from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from shutil import which

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

REPO = Path(__file__).resolve().parent
CACHE = REPO / ".dc1-cache"
RUNS = REPO / "runs" / "web"
SCRIPTS = REPO / "scripts"
FETCH_SCRIPT = SCRIPTS / "dc1_fetch_live.py"
FILL_SCRIPT = SCRIPTS / "fill_dc1_live.py"

app = FastAPI(title="DC1 Data Space Analysis Launcher")
templates = Jinja2Templates(directory=str(REPO / "templates"))

_lock = threading.Lock()
_active_run_id: str | None = None
_runs: dict[str, dict] = {}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_spaces_from_cache() -> list[str]:
    path = CACHE / "data-spaces.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []
    records = payload.get("records")
    if not isinstance(records, list):
        return []
    names = sorted({r.get("name", "").strip() for r in records if isinstance(r, dict) and r.get("name")})
    return [n for n in names if n]


def read_org_context() -> dict:
    sf_bin = which("sf")
    if not sf_bin:
        return {
            "ok": False,
            "error": "Salesforce CLI (sf) was not found in PATH. Open a terminal where sf works and start the web app from that same environment.",
        }
    cmd = [sf_bin, "org", "display", "--json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO), check=False)
    except OSError as exc:
        return {"ok": False, "error": f"Failed to run sf CLI: {exc}"}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip() or "sf org display failed"}
    try:
        body = json.loads(proc.stdout or "{}")
        res = body.get("result") or {}
    except ValueError:
        return {"ok": False, "error": "Unable to parse sf org display --json output"}
    return {
        "ok": True,
        "orgId": res.get("id"),
        "username": res.get("username"),
        "instanceUrl": res.get("instanceUrl"),
    }


def check_preflight() -> dict:
    template_path = REPO / "DC1_Data_Space_Analysis_Record_TEMPLATE.docx"
    checks = {
        "templateExists": template_path.exists(),
        "fetchScriptExists": FETCH_SCRIPT.exists(),
        "fillScriptExists": FILL_SCRIPT.exists(),
        "cacheExists": CACHE.exists(),
    }
    org = read_org_context()
    checks["cliAuthenticated"] = bool(org.get("ok"))
    return {"checks": checks, "org": org}


def append_log(run_id: str, line: str) -> None:
    with _lock:
        run = _runs.get(run_id)
        if not run:
            return
        run["logs"].append(line.rstrip())
        if len(run["logs"]) > 1000:
            run["logs"] = run["logs"][-1000:]


def run_command(run_id: str, step: str, cmd: list[str]) -> int:
    with _lock:
        _runs[run_id]["step"] = step
    append_log(run_id, f"$ {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
    except OSError as exc:
        append_log(run_id, f"[{step}] failed to start: {exc}")
        return 1
    assert proc.stdout is not None
    for line in proc.stdout:
        append_log(run_id, line)
    proc.wait()
    append_log(run_id, f"[{step}] exit_code={proc.returncode}")
    return int(proc.returncode)


def start_run(space: str, fresh_fetch: bool) -> str:
    global _active_run_id
    safe_space = re.sub(r"[^A-Za-z0-9_.-]+", "_", space)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    run_dir = RUNS / run_id
    docs_dir = run_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    with _lock:
        if _active_run_id:
            raise RuntimeError("Another run is currently in progress.")
        _active_run_id = run_id
        _runs[run_id] = {
            "id": run_id,
            "space": space,
            "safeSpace": safe_space,
            "status": "running",
            "step": "queued",
            "freshFetch": fresh_fetch,
            "logs": [],
            "startedAtUtc": now_utc(),
            "finishedAtUtc": None,
            "outputFile": None,
            "runDir": str(run_dir),
            "error": None,
        }

    def worker() -> None:
        global _active_run_id
        try:
            preflight = check_preflight()
            checks = preflight["checks"]
            if not all((checks["templateExists"], checks["fetchScriptExists"], checks["fillScriptExists"], checks["cliAuthenticated"])):
                raise RuntimeError(f"Preflight failed: {json.dumps(preflight, separators=(',', ':'))}")

            if fresh_fetch:
                rc = run_command(
                    run_id,
                    "fetch",
                    [
                        sys.executable,
                        "-u",
                        str(FETCH_SCRIPT),
                        "--clean-cache",
                        "--workers",
                        "8",
                        "--max-retries",
                        "4",
                        "--retry-base-ms",
                        "400",
                        "--adaptive-throttle",
                    ],
                )
                if rc != 0:
                    raise RuntimeError("Fetch step failed. See logs.")

            rc = run_command(
                run_id,
                "fill",
                [
                    sys.executable,
                    "-u",
                    str(FILL_SCRIPT),
                    "--space",
                    space,
                    "--output-dir",
                    str(docs_dir),
                ],
            )
            if rc != 0:
                raise RuntimeError("Fill step failed. See logs.")

            out_candidates = sorted(docs_dir.glob(f"DC1_Data_Space_Analysis_Record_{safe_space}_LIVE_*.docx"))
            output_file = str(out_candidates[-1]) if out_candidates else None

            with _lock:
                run = _runs[run_id]
                run["status"] = "completed"
                run["step"] = "done"
                run["finishedAtUtc"] = now_utc()
                run["outputFile"] = output_file
                if not output_file:
                    run["error"] = "Run finished but output file was not found."
                    run["status"] = "failed"
        except Exception as exc:
            with _lock:
                run = _runs[run_id]
                run["status"] = "failed"
                run["step"] = "error"
                run["finishedAtUtc"] = now_utc()
                run["error"] = str(exc)
                run["logs"].append(f"[error] {exc}")
        finally:
            with _lock:
                _active_run_id = None

    threading.Thread(target=worker, daemon=True).start()
    return run_id


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    # Use keyword arguments for compatibility across Starlette template APIs.
    return templates.TemplateResponse(request=request, name="index.html", context={})


@app.get("/api/preflight")
def api_preflight():
    return check_preflight()


@app.get("/api/spaces")
def api_spaces():
    spaces = load_spaces_from_cache()
    return {"spaces": spaces, "count": len(spaces)}


@app.post("/api/spaces/refresh")
def api_spaces_refresh():
    with _lock:
        if _active_run_id:
            raise HTTPException(status_code=409, detail="Cannot refresh while a run is active.")
    cmd = [
        sys.executable,
        "-u",
        str(FETCH_SCRIPT),
        "--clean-cache",
        "--workers",
        "8",
        "--max-retries",
        "4",
        "--retry-base-ms",
        "400",
        "--adaptive-throttle",
    ]
    proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=(proc.stdout or "") + "\n" + (proc.stderr or ""))
    return {"ok": True, "spaces": load_spaces_from_cache()}


@app.post("/api/run")
def api_run(payload: dict):
    space = str(payload.get("space", "")).strip()
    fresh_fetch = bool(payload.get("freshFetch", True))
    if not space:
        raise HTTPException(status_code=400, detail="space is required")
    if space not in load_spaces_from_cache() and not fresh_fetch:
        raise HTTPException(status_code=400, detail="Space not found in cache. Use freshFetch or refresh spaces first.")
    try:
        run_id = start_run(space=space, fresh_fetch=fresh_fetch)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"runId": run_id}


@app.get("/api/run/active")
def api_run_active():
    with _lock:
        return {"runId": _active_run_id}


@app.get("/api/runs/{run_id}")
def api_run_status(run_id: str):
    with _lock:
        run = _runs.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/api/runs/{run_id}/download")
def api_run_download(run_id: str):
    with _lock:
        run = _runs.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    output_file = run.get("outputFile")
    if not output_file:
        raise HTTPException(status_code=404, detail="No output file for this run")
    path = Path(output_file)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Output file path no longer exists")
    return FileResponse(path, filename=path.name, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
