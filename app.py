from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from shutil import which
import shutil
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook
from starlette.requests import Request

REPO = Path(__file__).resolve().parent
CACHE = REPO / ".data-space-analysis-cache"
RUNS = REPO / "runs" / "web"
SCRIPTS = REPO / "scripts"
FETCH_SCRIPT = SCRIPTS / "fetch_live.py"
FILL_SCRIPT = SCRIPTS / "fill_live.py"

app = FastAPI(title="Data Cloud Data Space Analysis Launcher")
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
    template_path = REPO / "DataCloud_DataSpace_Analysis_Record_TEMPLATE.docx"
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


def load_cache_json(name: str) -> dict:
    p = CACHE / f"{name}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def excel_safe_sheet_name(name: str, used: set[str]) -> str:
    cleaned = re.sub(r"[:\\/*?\[\]]", "_", name).strip() or "Sheet"
    base = cleaned[:31]
    candidate = base
    suffix = 1
    while candidate in used:
        tail = f"_{suffix}"
        candidate = (base[: 31 - len(tail)] + tail) if len(base) + len(tail) > 31 else (base + tail)
        suffix += 1
    used.add(candidate)
    return candidate


def cell_value(v: Any) -> str | int | float | bool:
    if isinstance(v, (str, int, float, bool)):
        return v
    if v is None:
        return ""
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def write_raw_json_sheet(wb: Workbook, sheet_name: str, payload: Any, used_names: set[str]) -> None:
    ws = wb.create_sheet(excel_safe_sheet_name(sheet_name, used_names))
    ws.append(["json"])
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    chunk_size = 32000  # keep under Excel's 32767 char cell limit
    for i in range(0, len(text), chunk_size):
        ws.append([text[i : i + chunk_size]])


def write_records_sheet(wb: Workbook, sheet_name: str, records: list[dict], used_names: set[str]) -> None:
    ws = wb.create_sheet(excel_safe_sheet_name(sheet_name, used_names))
    if not records:
        ws.append(["info"])
        ws.append(["No records"])
        return
    header: list[str] = []
    seen: set[str] = set()
    for rec in records:
        for key in rec.keys():
            if key not in seen:
                seen.add(key)
                header.append(key)
    ws.append(header)
    for rec in records:
        ws.append([cell_value(rec.get(col)) for col in header])


def build_cache_workbook(run_id: str, space: str, run_dir: Path) -> Path:
    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    summary.append(["Run ID", run_id])
    summary.append(["Data Space", space])
    summary.append(["Generated At UTC", now_utc()])
    summary.append(["Cache Directory", str(CACHE)])

    used_names = {"Summary"}
    cache_files = sorted(CACHE.glob("*.json"))
    summary.append(["Cache File Count", len(cache_files)])

    for f in cache_files:
        summary.append(["Cache File", f.name])
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            write_raw_json_sheet(wb, f"{f.stem}_raw", {"error": "Invalid JSON"}, used_names)
            continue

        records = payload.get("records") if isinstance(payload, dict) else None
        if isinstance(records, list) and records and all(isinstance(r, dict) for r in records):
            write_records_sheet(wb, f"{f.stem}_records", records, used_names)
        write_raw_json_sheet(wb, f"{f.stem}_raw", payload, used_names)

    out = run_dir / f"DataCloud_DataSpace_Metadata_Bundle_{space}_{run_id}.xlsx"
    wb.save(str(out))
    return out


def _new_lucid_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _cardinality_style(cardinality: str | None) -> tuple[str, str]:
    c = (cardinality or "").upper()
    if c in ("ONETOONE",):
        return "CFN ERD Exactly One Arrow", "CFN ERD Exactly One Arrow"
    if c in ("ONETON",):
        return "CFN ERD Exactly One Arrow", "CFN ERD Zero Or More Arrow"
    if c in ("NTOONE",):
        return "CFN ERD Zero Or More Arrow", "CFN ERD Exactly One Arrow"
    return "CFN ERD Zero Or More Arrow", "CFN ERD Zero Or More Arrow"


def collect_erd_graph_data() -> tuple[dict[str, tuple[str, str]], list[tuple[str, str, str, str | None]]]:
    streams = load_cache_json("data-streams").get("records", [])
    metadata_dlo = load_cache_json("metadata-dlo").get("records", [])
    metadata_dmo = load_cache_json("metadata-dmo").get("records", [])
    dmo_mappings = load_cache_json("dmo-mappings").get("byDmo", {})

    nodes: dict[str, tuple[str, str]] = {}  # apiName -> (type, label)
    edges: list[tuple[str, str, str, str | None]] = []  # src, dst, label, cardinality

    for s in streams:
        s_name = s.get("name")
        dlo = ((s.get("dataLakeObjectInfo") or {}).get("name"))
        if not s_name:
            continue
        nodes[s_name] = ("Data Stream", s_name)
        if dlo:
            nodes[dlo] = ("DLO", dlo)
            edges.append((s_name, dlo, "streams into", "NTOONE"))

    for d in metadata_dlo:
        name = d.get("name")
        if name:
            nodes.setdefault(name, ("DLO", name))

    for d in metadata_dmo:
        name = d.get("name")
        display = d.get("displayName") or name
        if name:
            nodes.setdefault(name, ("DMO", str(display)))

    for dmo_name, details in dmo_mappings.items():
        body = (details or {}).get("body") or {}
        for m in body.get("objectSourceTargetMaps") or []:
            src = m.get("sourceEntityDeveloperName")
            dst = m.get("targetEntityDeveloperName") or dmo_name
            if src and dst:
                nodes.setdefault(src, ("DLO", src))
                nodes.setdefault(dst, ("DMO", dst))
                edges.append((src, dst, "mapped to", "NTOONE"))

    for d in metadata_dmo:
        for rel in d.get("relationships") or []:
            src = rel.get("fromEntity")
            dst = rel.get("toEntity")
            if src and dst:
                nodes.setdefault(src, ("DMO", src))
                nodes.setdefault(dst, ("DMO", dst))
                edges.append((src, dst, "related to", rel.get("cardinality")))

    # De-duplicate edges while preserving order
    seen_edge: set[tuple[str, str, str, str | None]] = set()
    unique_edges: list[tuple[str, str, str, str | None]] = []
    for e in edges:
        if e not in seen_edge:
            seen_edge.add(e)
            unique_edges.append(e)
    return nodes, unique_edges


def build_lucid_erd_json(run_id: str, space: str, run_dir: Path) -> Path:
    prov = load_cache_json("_provenance")
    nodes, unique_edges = collect_erd_graph_data()

    shape_ids: dict[str, str] = {}
    shapes: list[dict] = []
    for api_name, (kind, label) in sorted(nodes.items(), key=lambda x: (x[1][0], x[0])):
        sid = _new_lucid_id("shape")
        shape_ids[api_name] = sid
        shapes.append(
            {
                "id": sid,
                "class": "SFACard",
                "textAreas": [
                    {
                        "label": "t_header",
                        "text": f"{kind}\u2028{label}",
                    }
                ],
                "customData": [],
                "linkedData": [],
            }
        )

    lines: list[dict] = []
    for src, dst, label, cardinality in unique_edges:
        src_id = shape_ids.get(src)
        dst_id = shape_ids.get(dst)
        if not src_id or not dst_id:
            continue
        e1_style, e2_style = _cardinality_style(cardinality)
        lines.append(
            {
                "id": _new_lucid_id("line"),
                "endpoint1": {"style": e1_style, "connectedTo": src_id},
                "endpoint2": {"style": e2_style, "connectedTo": dst_id},
                "textAreas": [{"label": "t0", "text": label}],
                "customData": [],
                "linkedData": [],
            }
        )

    chart = {
        "id": _new_lucid_id("doc"),
        "title": f"Data Cloud Data Model ERD - {space}",
        "product": "lucidchart",
        "pages": [
            {
                "id": _new_lucid_id("page"),
                "title": "Data Cloud ERD",
                "index": 0,
                "items": {
                    "shapes": shapes,
                    "lines": lines,
                    "groups": [],
                    "layers": [],
                },
                "customData": [],
                "linkedData": [],
            }
        ],
        "data": {"collections": []},
        "accountId": 0,
        "metadata": {
            "runId": run_id,
            "space": space,
            "orgId": prov.get("orgId"),
            "generatedAtUtc": now_utc(),
            "nodeCount": len(shapes),
            "edgeCount": len(lines),
        },
    }

    out = run_dir / f"DataCloud_DataSpace_ERD_{space}_{run_id}.json"
    out.write_text(json.dumps(chart, indent=2), encoding="utf-8")
    return out


def build_drawio_erd_xml(run_id: str, space: str, run_dir: Path) -> Path:
    nodes, edges = collect_erd_graph_data()
    ordered_nodes = sorted(nodes.items(), key=lambda x: (x[1][0], x[0]))
    node_ids: dict[str, str] = {}
    xml_parts: list[str] = []

    xml_parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    xml_parts.append('<mxfile host="app.diagrams.net" modified="" agent="DataCloudDataSpaceAnalysis" version="22.0.0">')
    xml_parts.append(f'  <diagram id="erd_{run_id}" name="Data Cloud ERD {html.escape(space)}">')
    xml_parts.append('    <mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="2200" pageHeight="1600" math="0" shadow="0">')
    xml_parts.append("      <root>")
    xml_parts.append('        <mxCell id="0"/>')
    xml_parts.append('        <mxCell id="1" parent="0"/>')

    # Basic grid layout
    cols = 5
    x0, y0 = 40, 40
    x_step, y_step = 380, 130
    for idx, (api_name, (kind, label)) in enumerate(ordered_nodes):
        cell_id = f"n{idx + 1}"
        node_ids[api_name] = cell_id
        row = idx // cols
        col = idx % cols
        x = x0 + col * x_step
        y = y0 + row * y_step
        text = html.escape(f"{kind}: {label}")
        xml_parts.append(
            f'        <mxCell id="{cell_id}" value="{text}" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#EAF5FF;strokeColor=#0176D3;fontSize=11;" vertex="1" parent="1">'
        )
        xml_parts.append(f'          <mxGeometry x="{x}" y="{y}" width="340" height="90" as="geometry"/>')
        xml_parts.append("        </mxCell>")

    for i, (src, dst, rel_label, cardinality) in enumerate(edges, start=1):
        src_id = node_ids.get(src)
        dst_id = node_ids.get(dst)
        if not src_id or not dst_id:
            continue
        label = html.escape(rel_label or "")
        style = "endArrow=block;endFill=1;html=1;strokeColor=#6B7280;"
        if (cardinality or "").upper() in ("ONETOONE",):
            style = "endArrow=block;startArrow=block;endFill=1;startFill=1;html=1;strokeColor=#6B7280;"
        xml_parts.append(
            f'        <mxCell id="e{i}" value="{label}" style="{style}" edge="1" parent="1" source="{src_id}" target="{dst_id}">'
        )
        xml_parts.append('          <mxGeometry relative="1" as="geometry"/>')
        xml_parts.append("        </mxCell>")

    xml_parts.append("      </root>")
    xml_parts.append("    </mxGraphModel>")
    xml_parts.append("  </diagram>")
    xml_parts.append("</mxfile>")

    out = run_dir / f"DataCloud_DataSpace_ERD_{space}_{run_id}.drawio"
    out.write_text("\n".join(xml_parts), encoding="utf-8")
    return out


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
            "workbookFile": None,
            "erdFile": None,
            "drawioFile": None,
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

            out_candidates = sorted(docs_dir.glob(f"DataCloud_DataSpace_Analysis_Record_{safe_space}_LIVE_*.docx"))
            output_file = str(out_candidates[-1]) if out_candidates else None
            if output_file:
                source = Path(output_file)
                renamed = docs_dir / f"DataCloud_DataSpace_Analysis_Record_{safe_space}_{run_id}.docx"
                shutil.copy2(source, renamed)
                output_file = str(renamed)
            workbook_file = str(build_cache_workbook(run_id=run_id, space=safe_space, run_dir=run_dir))
            erd_file = str(build_lucid_erd_json(run_id=run_id, space=safe_space, run_dir=run_dir))
            drawio_file = str(build_drawio_erd_xml(run_id=run_id, space=safe_space, run_dir=run_dir))

            with _lock:
                run = _runs[run_id]
                run["status"] = "completed"
                run["step"] = "done"
                run["finishedAtUtc"] = now_utc()
                run["outputFile"] = output_file
                run["workbookFile"] = workbook_file
                run["erdFile"] = erd_file
                run["drawioFile"] = drawio_file
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


@app.get("/api/runs/{run_id}/download-workbook")
def api_run_download_workbook(run_id: str):
    with _lock:
        run = _runs.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    workbook_file = run.get("workbookFile")
    if not workbook_file:
        raise HTTPException(status_code=404, detail="No workbook file for this run")
    path = Path(workbook_file)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Workbook file path no longer exists")
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/runs/{run_id}/download-erd")
def api_run_download_erd(run_id: str):
    with _lock:
        run = _runs.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    erd_file = run.get("erdFile")
    if not erd_file:
        raise HTTPException(status_code=404, detail="No ERD file for this run")
    path = Path(erd_file)
    if not path.exists():
        raise HTTPException(status_code=404, detail="ERD file path no longer exists")
    return FileResponse(path, filename=path.name, media_type="application/json")


@app.get("/api/runs/{run_id}/download-drawio")
def api_run_download_drawio(run_id: str):
    with _lock:
        run = _runs.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    drawio_file = run.get("drawioFile")
    if not drawio_file:
        raise HTTPException(status_code=404, detail="No Draw.io file for this run")
    path = Path(drawio_file)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Draw.io file path no longer exists")
    return FileResponse(path, filename=path.name, media_type="application/xml")
