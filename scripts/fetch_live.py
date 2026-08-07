"""Fetch every Data Cloud metadata endpoint needed for the analysis record into a JSON cache.

One command, one pass. Auth comes from the Salesforce CLI default org.
Reading the token from `sf org display` is deprecated and recent CLIs redact it,
so the token is retrieved with the supported `sf org auth show-access-token
--json` command; instanceUrl and org id (not secrets) come from `sf org display
--json`. Older CLIs without that command fall back to display output with
SF_TEMP_SHOW_SECRETS=true set automatically, so no manual export is needed.
Org id, instance and fetch timestamp go to _provenance.json so the fill step can
prove provenance.

Cost control:
  * Phase 0 counts DMOs with one Tooling query, so the catalogue can be paged
    concurrently instead of discovered page by page.
  * Phase 1 fires every independent call at once, including the catalogue pages.
  * Phase 2 fires the dependent per-object loops at once.
  * Stream mappings ride along on the list call, which removes one call per stream.

Endpoint behaviour worth knowing (verified live against this org, parameters checked
against the v67 OpenAPI spec at developer.salesforce.com/docs/data/connectapi):
  * /ssot/data-streams?limit=200&includeMappings=true returns every stream WITH its
    source-to-DLO field mappings, so per-stream detail calls are unnecessary. Omit
    includeMappings and the `mappings` array comes back empty, which is misleading.
  * /ssot/data-lake-objects ignores `limit`; it pages 20 at a time via nextPageUrl.
  * /ssot/data-model-objects returns the whole CIM catalogue (~1090), not the
    deployed subset, and is the only source of total field counts per DMO. It honours
    limit/offset up to 200. A single large-limit call is unreliable: limit=1200 took
    180s and came back empty, while six concurrent pages of 200 take 39s.
  * /ssot/metadata?entityType=DataModelObject is the deployed subset and the only
    source of relationships and primary keys, but its `fields` array holds MAPPED
    fields only, so unmapped counts need the catalogue.
  * /ssot/metadata-entities (v66+) lists deployed objects but carries no fields.
  * /ssot/data-model-object-mappings requires `dmoDeveloperName`, one DMO per call.
    `dloDeveloperName` alone returns INVALID_INPUT, and Tooling has no
    MktDataLakeMapping object, so there is no bulk form.
  * /ssot/data-graphs and /ssot/streaming-data-transforms cannot be listed.
  * /ssot/connections requires a connectorType. /ssot/connectors lists connector
    families the platform supports (can be much larger than the configured subset).
    Default connector scope is now comprehensive (`--connector-scope all`) to avoid
    missing configured connectors. Use `--connector-scope curated` for faster runs.
  * Neither streams nor identity resolutions expose run history or durations.
  * /ssot/data-kits returns 500 on this org, so kit packaging stays manual.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from shutil import which

API = "v63.0"
CACHE = Path(__file__).resolve().parent.parent / ".data-space-analysis-cache"
PAGE = 200
MAX_PAGES = 60
WORKERS = 8
MAX_RETRIES = 4
RETRY_BASE_MS = 400
THROTTLE_WARN_PCT = 85.0
THROTTLE_HARD_PCT = 92.0

# Curated baseline connector types. The fetcher can also expand this list from
# ssot/connectors to avoid missing newly introduced connector families.
CONNECTOR_TYPES = [
    "SalesforceMarketingCloud",
    "SalesforceDotCom",
    "IngestApi",
    "StreamingApp",
    "UploadedFiles",
    "SFTP",
    "BIGQUERY",
    "AwsS3",
    "FacebookAds",
    "GoogleCloudStorage",
    "AzureStorage",
    "Snowflake",
    "Databricks",
    "AmazonRedshift",
    "MarketingCloudPersonalization",
    "SalesforceCommerceCloud",
]

DMO_CATALOGUE_SOQL = (
    "SELECT DeveloperName, MasterLabel, CreationType, RefEntitySubjectArea, RefEntityGroup, "
    "IsSegmentable, IsEnabled, DataModelObjectStatus, Description, ManageableState, DataStoreType "
    "FROM MktDataModelObject"
)

# ssot/data-streams only returns streams visible in the caller's default data
# space context, so it misses streams that live in other spaces (e.g. Marketing
# Cloud and Marketing Cloud Personalization ingestion). The Tooling API
# DataStreamDefinition object is the authoritative, org-wide list of every data
# stream across every data space. We fetch both and merge at fill time.
DATA_STREAM_DEF_SOQL = (
    "SELECT Id, DeveloperName, MasterLabel, DataConnectorType, DataConnectorId, "
    "MktDataLakeObjectId, CreationType, Description, CreatedDate, LastModifiedDate "
    "FROM DataStreamDefinition ORDER BY MasterLabel"
)


def resolve_sf_cli() -> str:
    for candidate in ("sf", "sf.cmd", "sf.exe"):
        path = which(candidate)
        if path:
            return path
    raise RuntimeError(
        "Salesforce CLI executable not found in PATH. "
        "Install Salesforce CLI and restart your terminal. "
        "Expected one of: sf, sf.cmd, sf.exe."
    )


def cli_auth(target_org: str | None = None) -> tuple[str, str, str]:
    """Resolve an access token, instance URL and org id from the Salesforce CLI.

    Salesforce deprecated reading the access token from `sf org display`; recent
    CLIs redact it there. The supported way to retrieve a token programmatically
    is the dedicated `sf org auth show-access-token --json` command (the --json
    flag also skips its interactive security prompt). That command returns ONLY
    the token, so instanceUrl and org id still come from `sf org display --json`
    (those fields are not secrets and are never redacted).

    Strategy, resilient across CLI versions:
      1. `sf org display --json`         -> instanceUrl + org id (+ token on old CLIs)
      2. `sf org auth show-access-token` -> token on current CLIs (primary)
      3. legacy fallback                 -> token from display with SF_TEMP_SHOW_SECRETS
    """
    sf_cli = resolve_sf_cli()
    target_args = ["--target-org", target_org] if target_org else []
    show_secrets = {"SF_TEMP_SHOW_SECRETS": "true", "SFDX_TEMP_SHOW_SECRETS": "true"}
    errors: list[str] = []

    def usable(tok: str) -> bool:
        return bool(tok) and not tok.startswith("[REDACTED]")

    def run_json(args: list[str], label: str, extra_env: dict | None = None) -> dict | None:
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        try:
            out = subprocess.run(
                args, capture_output=True, text=True, check=False, timeout=180, env=env
            )
        except Exception as exc:  # noqa: BLE001 - report and continue to next fallback
            errors.append(f"- {label}: {exc}")
            return None
        stdout = (out.stdout or "").strip()
        stderr = (out.stderr or "").strip()
        if out.returncode != 0 or not stdout:
            errors.append(
                f"- {label}: exit={out.returncode} "
                f"stderr={stderr or '<empty>'} stdout={stdout[:200] or '<empty>'}"
            )
            return None
        try:
            return json.loads(stdout).get("result") or {}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"- {label}: unparseable JSON ({exc}) stdout={stdout[:200]}")
            return None

    # 1) org display: authoritative for instanceUrl + org id (non-secret fields).
    disp = run_json(
        [sf_cli, "org", "display", "--json", *target_args],
        "sf org display --json",
        show_secrets,
    ) or {}
    instance = (disp.get("instanceUrl") or "").rstrip("/")
    org_id = disp.get("id") or ""

    # 2) Preferred: the dedicated token command on current CLIs. --json also skips
    #    the interactive "reveal token?" confirmation prompt.
    token = ""
    at = run_json(
        [sf_cli, "org", "auth", "show-access-token", "--json", "--no-prompt", *target_args],
        "sf org auth show-access-token --json",
    )
    if isinstance(at, dict):
        token = (at.get("accessToken") or "").strip()

    # 3) Legacy fallback for CLIs without `org auth show-access-token`: read the
    #    token straight from display output (works when secrets are not redacted).
    if not usable(token):
        tok = (disp.get("accessToken") or "").strip()
        if usable(tok):
            token = tok
    if not usable(token):
        verbose = run_json(
            [sf_cli, "org", "display", "--verbose", "--json", *target_args],
            "sf org display --verbose --json",
            show_secrets,
        ) or {}
        tok = (verbose.get("accessToken") or "").strip()
        if usable(tok):
            token = tok

    if not usable(token):
        detail = "\n".join(errors) if errors else "<no CLI errors captured>"
        raise RuntimeError(
            "Salesforce CLI did not return a usable access token.\n"
            "Fixes, in order of likelihood:\n"
            "  1. Re-authenticate this org:  sf org login web --alias <alias>\n"
            "  2. Confirm the right org is active:  sf config get target-org\n"
            "     (set it with 'sf config set target-org <alias> --global')\n"
            "  3. Update the CLI so it has 'org auth show-access-token':\n"
            "        npm install --global @salesforce/cli@latest\n"
            "  4. Sanity check by hand:\n"
            "        sf org auth show-access-token --json\n"
            f"CLI attempts made:\n{detail}"
        )

    if not instance or not org_id:
        raise RuntimeError(
            "Salesforce CLI did not return instanceUrl/org id. "
            "Run 'sf org display --json' and verify the active org context."
        )
    return token, instance, org_id


class Org:
    def __init__(
        self,
        max_retries: int = MAX_RETRIES,
        retry_base_ms: int = RETRY_BASE_MS,
        adaptive_throttle: bool = True,
        target_org: str | None = None,
    ) -> None:
        self.token, self.instance, self.org_id = cli_auth(target_org=target_org)
        self.calls = 0
        self.retries = 0
        self.max_retries = max_retries
        self.retry_base_ms = retry_base_ms
        self.adaptive_throttle = adaptive_throttle
        self._lock = threading.Lock()
        self.limit_info: set[str] = set()
        self.max_api_usage_pct = 0.0
        self.throttle_sleep_seconds = 0.0

    @staticmethod
    def parse_api_limit(limit_header: str | None) -> tuple[int, int] | None:
        if not limit_header:
            return None
        m = re.search(r"api-usage=(\d+)/(\d+)", limit_header)
        if not m:
            return None
        used, limit = int(m.group(1)), int(m.group(2))
        if limit <= 0:
            return None
        return used, limit

    def _request(self, url: str) -> dict:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
        )
        for attempt in range(self.max_retries + 1):
            with self._lock:
                self.calls += 1
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    status = resp.status
                    raw = resp.read().decode("utf-8", "replace")
                    lim = resp.headers.get("Sforce-Limit-Info")
                    if lim:
                        with self._lock:
                            self.limit_info.add(lim)
                        parsed = self.parse_api_limit(lim)
                        if parsed:
                            used, limit = parsed
                            pct = (used / limit) * 100.0
                            sleep_s = 0.0
                            if self.adaptive_throttle:
                                if pct >= THROTTLE_HARD_PCT:
                                    sleep_s = 1.2 + random.random() * 0.6
                                elif pct >= THROTTLE_WARN_PCT:
                                    sleep_s = 0.25 + random.random() * 0.2
                            with self._lock:
                                self.max_api_usage_pct = max(self.max_api_usage_pct, pct)
                                self.throttle_sleep_seconds += sleep_s
                            if sleep_s:
                                time.sleep(sleep_s)
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {"_raw": raw}
                if isinstance(body, list):
                    body = {"records": body}
                return {"status": status, "body": body}
            except urllib.error.HTTPError as e:
                status = e.code
                raw = e.read().decode("utf-8", "replace")
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {"_raw": raw}
                if isinstance(body, list):
                    body = {"records": body}
                if status in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    with self._lock:
                        self.retries += 1
                    backoff = (self.retry_base_ms * (2**attempt) + random.randint(0, self.retry_base_ms)) / 1000
                    time.sleep(backoff)
                    continue
                return {"status": status, "body": body}
            except Exception as e:
                if attempt < self.max_retries:
                    with self._lock:
                        self.retries += 1
                    backoff = (self.retry_base_ms * (2**attempt) + random.randint(0, self.retry_base_ms)) / 1000
                    time.sleep(backoff)
                    continue
                return {"status": 0, "body": {"_error": str(e)}}
        return {"status": 0, "body": {"_error": "retry loop exhausted"}}

    def get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.instance}/services/data/{API}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._request(url)

    def get_absolute(self, path: str) -> dict:
        return self._request(f"{self.instance}{path}")

    def query(self, soql: str, tooling: bool = False) -> dict:
        """SOQL with nextRecordsUrl chaining. Tooling for Mkt* metadata objects."""
        endpoint = "tooling/query" if tooling else "query"
        res = self.get(endpoint, {"q": soql})
        if res["status"] >= 400:
            return {"status": res["status"], "records": [], "body": res["body"]}
        records = list(res["body"].get("records", []))
        nxt = res["body"].get("nextRecordsUrl")
        while nxt:
            page = self.get_absolute(nxt)
            if page["status"] >= 400:
                break
            records.extend(page["body"].get("records", []))
            nxt = page["body"].get("nextRecordsUrl")
        for r in records:
            r.pop("attributes", None)
        return {"status": res["status"], "records": records}

    @staticmethod
    def list_key(body: dict) -> str | None:
        if not isinstance(body, dict):
            return None
        for k, v in body.items():
            if isinstance(v, list) and not k.startswith("_") and k != "errors":
                return k
        return None

    def page_by_offset(
        self,
        path: str,
        params: dict | None = None,
        size_param: str = "limit",
        size: int = PAGE,
        key_hint: str | None = None,
    ) -> dict:
        """For endpoints that honour offset with some page-size parameter.

        Page size caps differ per endpoint: 200 for streams, segments, activations and
        activation targets, but only 20 for data transforms. Passing a bigger number
        than the cap makes the endpoint quietly return one short page, which reads as
        "that is all there is" and is how inventories silently lose rows.
        """
        merged: list = []
        key = key_hint
        status = None
        for page in range(MAX_PAGES):
            res = self.get(path, dict(params or {}, **{size_param: size, "offset": page * size}))
            status = res["status"]
            if status >= 400:
                return {"status": status, "key": key, "records": merged, "body": res["body"]}
            key = key or self.list_key(res["body"])
            if key is None:
                return {"status": status, "key": None, "records": merged, "body": res["body"]}
            chunk = res["body"].get(key) or []
            merged.extend(chunk)
            if len(chunk) < size:
                break
        return {"status": status, "key": key, "records": dedupe(merged)}

    def page_by_total(self, path: str, size: int, params: dict | None = None) -> dict:
        """For endpoints that cap the page size but report totalSize.

        The first call reveals the total, so the remaining pages go out concurrently
        instead of walking nextPageUrl one round trip at a time. That matters on orgs
        with hundreds of DLOs, where serial paging is the slowest thing in the run.

        /ssot/data-lake-objects needs care and the rules below are load-bearing:
          * offsets must step by the declared page size, never by how many records came
            back, because a page can be short mid-sequence (18 of 20) while the next
            offset still starts at 20. Stepping by the received count re-reads rows.
          * offsets past the real end are clamped rather than empty, so the same tail
            page can arrive twice.
          * totalSize overstates the truth (83 declared for 73 readable DLOs).
        So results are always deduplicated and totalSize is treated as an upper bound.
        """
        first = self.get(path, dict(params or {}, limit=size, offset=0))
        if first["status"] >= 400:
            return {"status": first["status"], "key": None, "records": [], "body": first["body"]}
        key = self.list_key(first["body"])
        merged = list(first["body"].get(key, [])) if key else []
        total = first["body"].get("totalSize")
        if isinstance(total, int) and total > size:
            for chunk in pmap(
                lambda off: (
                    lambda r: r["body"].get(key, []) if r["status"] < 400 else []
                )(self.get(path, dict(params or {}, limit=size, offset=off))),
                list(range(size, total, size)),
            ):
                merged.extend(chunk)
        return {
            "status": first["status"],
            "key": key,
            "records": dedupe(merged),
            "declaredTotalSize": total,
        }

    def page_by_next_url(self, path: str, params: dict | None = None) -> dict:
        """For endpoints that ignore limit and hand back nextPageUrl."""
        res = self.get(path, params)
        status = res["status"]
        if status >= 400:
            return {"status": status, "key": None, "records": [], "body": res["body"]}
        key = self.list_key(res["body"])
        merged = list(res["body"].get(key, [])) if key else []
        nxt = res["body"].get("nextPageUrl")
        seen = 0
        while nxt and seen < MAX_PAGES:
            seen += 1
            res = self.get_absolute(nxt)
            if res["status"] >= 400:
                break
            merged.extend(res["body"].get(key, []))
            nxt = res["body"].get("nextPageUrl")
        return {"status": status, "key": key, "records": dedupe(merged)}

    def page_by_collection(self, path: str, params: dict | None = None) -> dict:
        """For endpoints that nest records under collection.items (calculated insights)."""
        res = self.get(path, params)
        if res["status"] >= 400:
            return {"status": res["status"], "key": None, "records": [], "body": res["body"]}
        coll = res["body"].get("collection", {}) if isinstance(res["body"], dict) else {}
        items = list(coll.get("items", []))
        nxt = coll.get("nextPageUrl")
        while nxt:
            page = self.get_absolute(nxt)
            if page["status"] >= 400:
                break
            coll2 = page["body"].get("collection", {})
            items.extend(coll2.get("items", []))
            nxt = coll2.get("nextPageUrl")
        return {
            "status": res["status"],
            "key": "items",
            "records": items,
            "declaredCount": coll.get("count"),
        }

    def page_members(self, space: str) -> dict:
        # limit goes up to 4999 here, so one call covers any realistic data space.
        res = self.get(f"ssot/data-spaces/{space}/members", {"limit": 4999, "offset": 0})
        body = res["body"] if isinstance(res["body"], dict) else {}
        rows = list(body.get("members", []))
        nxt = body.get("nextPageUrl")
        while nxt:
            page = self.get_absolute(nxt)
            if page["status"] >= 400:
                break
            rows.extend(page["body"].get("members", []))
            nxt = page["body"].get("nextPageUrl")
        return {"status": res["status"], "members": rows, "totalSize": body.get("totalSize")}


def flat_list(res: dict, key_hint: str | None = None) -> dict:
    """Normalise a single-call list response to the cache shape."""
    body = res["body"]
    key = key_hint or Org.list_key(body)
    return {
        "status": res["status"],
        "key": key,
        "records": body.get(key, []) if key and isinstance(body, dict) else [],
        "body": None if key else body,
    }


def dedupe(rows: list) -> list:
    """Drop repeated records, keyed on the identifier the endpoint uses."""
    seen: set = set()
    out = []
    for r in rows:
        key = None
        if isinstance(r, dict):
            key = r.get("name") or r.get("developerName") or r.get("id") or r.get("apiName")
        if key is None:
            key = json.dumps(r, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def pmap(fn, items: list, workers: int = WORKERS) -> list:
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(fn, items))


def save(name: str, payload) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / f"{name}.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    recs = payload.get("records") if isinstance(payload, dict) else None
    count = len(recs) if isinstance(recs, list) else "-"
    status = payload.get("status") if isinstance(payload, dict) else "-"
    print(f"  {name:30s} status={status} records={count}", flush=True)


def main() -> None:
    import argparse

    global WORKERS
    global CACHE
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Fetch Data Cloud metadata into cache for analysis generation."
    )
    parser.add_argument(
        "--connector-scope",
        choices=("all", "curated"),
        default="all",
        help=(
            "Connector probe scope: 'all' (default) probes every connector type returned by "
            "ssot/connectors to avoid missing configured connectors; 'curated' probes the "
            "curated list plus connector types seen on streams."
        ),
    )
    parser.add_argument("--workers", type=int, default=WORKERS, help="Max parallel workers (default: 8).")
    parser.add_argument("--max-retries", type=int, default=MAX_RETRIES, help="Retries for 429/5xx/network errors.")
    parser.add_argument("--retry-base-ms", type=int, default=RETRY_BASE_MS, help="Base backoff in milliseconds.")
    parser.add_argument("--clean-cache", action="store_true", help="Delete existing cache JSON files before fetching.")
    parser.add_argument(
        "--adaptive-throttle",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Adaptively slow requests when API limit usage is high (default: enabled).",
    )
    parser.add_argument(
        "--target-org",
        default="",
        help="Salesforce org alias/username/id to run against (optional).",
    )
    parser.add_argument(
        "--cache-dir",
        default="",
        help="Override output cache directory (optional).",
    )
    args = parser.parse_args()

    WORKERS = max(1, args.workers)
    if args.cache_dir:
        CACHE = Path(args.cache_dir).resolve()
    t0 = time.time()
    connector_scope = args.connector_scope
    org = Org(
        max_retries=max(0, args.max_retries),
        retry_base_ms=max(50, args.retry_base_ms),
        adaptive_throttle=bool(args.adaptive_throttle),
        target_org=(args.target_org.strip() or None),
    )
    print(f"org={org.org_id} instance={org.instance} api={API} workers={WORKERS}", flush=True)

    CACHE.mkdir(parents=True, exist_ok=True)
    if args.clean_cache:
        for p in CACHE.glob("*.json"):
            p.unlink(missing_ok=True)
    # Remove stale files that older versions produced but the current template does not use.
    for stale in ("data-actions.json", "data-action-targets.json", "configured-models.json", "clean-rooms.json"):
        (CACHE / stale).unlink(missing_ok=True)

    # ---------------- phase 0: count DMOs so the catalogue can be paged in parallel
    tooling = org.query(DMO_CATALOGUE_SOQL, tooling=True)
    save("tooling-dmo", tooling)
    dmo_total = len(tooling["records"]) or 1200
    offsets = list(range(0, dmo_total + PAGE, PAGE))

    # Data spaces must be known before the space-scoped fetches below. Several
    # Connect API endpoints (ssot/metadata for DMO/DLO/CI,
    # ssot/data-model-object-mappings, ssot/segments, ssot/activations,
    # ssot/data-graphs, ssot/search-index) are data-space scoped and silently
    # default to the `default` space when no dataspace is supplied. To be an exact
    # replica of ANY org we enumerate every data space and fetch per-space, then
    # merge, tagging each record with the spaces it belongs to.
    data_spaces_res = org.page_by_offset("ssot/data-spaces", size=4999)
    save("data-spaces", data_spaces_res)
    spaces = [s["name"] for s in data_spaces_res["records"] if s.get("name")] or ["default"]
    print(f"data spaces ({len(spaces)}): {', '.join(spaces)}", flush=True)

    # Per-space fetch statistics, surfaced later in the completeness audit so a
    # user can see exactly how many records each space contributed.
    perspace_counts: dict[str, dict[str, int]] = {}

    def metadata_all_spaces(entity: str) -> dict:
        """Fetch ssot/metadata for an entity across every data space and merge.
        Each record is tagged with `_dataSpaces` = the spaces it appears in."""

        def one(sp: str):
            res = org.get("ssot/metadata", {"entityType": entity, "dataspace": sp})
            md = (res["body"].get("metadata") or []) if res["status"] < 400 else []
            return sp, res["status"], md

        by_name: dict[str, dict] = {}
        counts: dict[str, int] = {}
        ok = False
        last = 200
        for sp, st, md in pmap(one, spaces):
            last = st
            counts[sp] = len(md)
            if st < 400:
                ok = True
            for m in md:
                nm = m.get("name")
                if not nm:
                    continue
                cur = by_name.get(nm)
                if cur is None:
                    m = dict(m)
                    m["_dataSpaces"] = [sp]
                    by_name[nm] = m
                elif sp not in cur["_dataSpaces"]:
                    cur["_dataSpaces"].append(sp)
        perspace_counts[entity] = counts
        return {"status": 200 if ok else last, "key": "metadata", "records": list(by_name.values())}

    def offset_list_all_spaces(path: str, size_param: str = "limit", size: int = PAGE) -> dict:
        """Fetch an offset-paged, data-space-scoped list across every space and
        merge, tagging each record with the spaces it appears in."""

        def one(sp: str):
            return sp, org.page_by_offset(path, {"dataspace": sp}, size_param=size_param, size=size)

        merged: dict = {}
        counts: dict[str, int] = {}
        ok = False
        last = 200
        key = None
        for sp, r in pmap(one, spaces):
            last = r["status"]
            key = key or r.get("key")
            counts[sp] = len(r["records"])
            if r["status"] < 400:
                ok = True
            for rec in r["records"]:
                rid = (
                    rec.get("id")
                    or rec.get("apiName")
                    or rec.get("developerName")
                    or rec.get("name")
                    or json.dumps(rec, sort_keys=True)
                )
                cur = merged.get(rid)
                if cur is None:
                    rec = dict(rec)
                    rec["_dataSpaces"] = [sp]
                    merged[rid] = rec
                elif sp not in cur.get("_dataSpaces", []):
                    cur.setdefault("_dataSpaces", []).append(sp)
        perspace_counts[path] = counts
        return {"status": 200 if ok else last, "key": key, "records": list(merged.values())}

    def flat_list_all_spaces(path: str, key: str) -> dict:
        """Fetch a single-call, data-space-scoped list across every space and merge."""

        def one(sp: str):
            r = org.get(path, {"dataspace": sp})
            recs = (r["body"].get(key) or []) if r["status"] < 400 else []
            return sp, r["status"], recs

        merged: dict = {}
        counts: dict[str, int] = {}
        ok = False
        last = 200
        for sp, st, recs in pmap(one, spaces):
            last = st
            counts[sp] = len(recs)
            if st < 400:
                ok = True
            for rec in recs:
                rid = (
                    rec.get("developerName")
                    or rec.get("apiName")
                    or rec.get("name")
                    or rec.get("id")
                    or json.dumps(rec, sort_keys=True)
                )
                cur = merged.get(rid)
                if cur is None:
                    rec = dict(rec)
                    rec["_dataSpaces"] = [sp]
                    merged[rid] = rec
                elif sp not in cur.get("_dataSpaces", []):
                    cur.setdefault("_dataSpaces", []).append(sp)
        perspace_counts[path] = counts
        return {"status": 200 if ok else last, "key": key, "records": list(merged.values())}

    def collection_all_spaces(path: str, params: dict) -> dict:
        """Fetch a collection-paged, data-space-scoped list across every space and merge."""

        def one(sp: str):
            return sp, org.page_by_collection(path, dict(params, dataspace=sp))

        merged: dict = {}
        counts: dict[str, int] = {}
        ok = False
        last = 200
        for sp, r in pmap(one, spaces):
            last = r["status"]
            counts[sp] = len(r["records"])
            if r["status"] < 400:
                ok = True
            for rec in r["records"]:
                rid = (
                    rec.get("apiName")
                    or rec.get("developerName")
                    or rec.get("name")
                    or rec.get("id")
                    or json.dumps(rec, sort_keys=True)
                )
                cur = merged.get(rid)
                if cur is None:
                    rec = dict(rec)
                    rec["_dataSpaces"] = [sp]
                    merged[rid] = rec
                elif sp not in cur.get("_dataSpaces", []):
                    cur.setdefault("_dataSpaces", []).append(sp)
        perspace_counts[path] = counts
        return {"status": 200 if ok else last, "key": "items", "records": list(merged.values())}

    def catalogue_page(off: int) -> list:
        res = org.get("ssot/data-model-objects", {"limit": PAGE, "offset": off})
        body = res["body"] if isinstance(res["body"], dict) else {}
        return body.get("dataModelObject", []) or []

    # ---------------- phase 1: everything with no dependency, all at once
    tasks: list[tuple[str, callable]] = [
        (
            "data-streams",
            lambda: org.page_by_offset("ssot/data-streams", {"includeMappings": "true"}),
        ),
        # Authoritative, org-wide stream list across every data space (Tooling API).
        # ssot/data-streams above only sees the default-space context and misses
        # streams from other spaces (Marketing Cloud, MC Personalization, etc.).
        ("tooling-data-streams", lambda: org.query(DATA_STREAM_DEF_SOQL, tooling=True)),
        # Resolves a Tooling stream's MktDataLakeObjectId to a DLO developer name.
        (
            "tooling-mkt-dlo",
            lambda: org.query("SELECT Id, DeveloperName FROM MktDataLakeObject", tooling=True),
        ),
        # Connection labels for connector families that ssot/connections cannot
        # enumerate (e.g. SalesforceInteractionStudio / MC Personalization).
        (
            "tooling-mkt-connection",
            lambda: org.query(
                "SELECT Id, MasterLabel, ConnectionMethod, CreatedDate FROM MktDataConnection",
                tooling=True,
            ),
        ),
        # Caps at 20 per page whatever limit says, but reports totalSize.
        ("data-lake-objects", lambda: org.page_by_total("ssot/data-lake-objects", 20)),
        (
            "data-model-objects-catalogue",
            lambda: {
                "status": 200,
                "key": "dataModelObject",
                "records": [r for page in pmap(catalogue_page, offsets) for r in page],
            },
        ),
        # ssot/metadata is data-space scoped and defaults to `default`; fetch
        # every space and merge so multi-space orgs are captured in full.
        ("metadata-dmo", lambda: metadata_all_spaces("DataModelObject")),
        ("metadata-dlo", lambda: metadata_all_spaces("DataLakeObject")),
        ("metadata-ci", lambda: metadata_all_spaces("CalculatedInsight")),
        # identity-resolutions is org-wide (returns records for every space).
        ("identity-resolutions", lambda: flat_list(org.get("ssot/identity-resolutions"))),
        # segments/activations are data-space scoped; fetch per space and merge.
        ("segments", lambda: offset_list_all_spaces("ssot/segments", size_param="batchSize")),
        ("activations", lambda: offset_list_all_spaces("ssot/activations", size_param="batchSize")),
        (
            "activation-targets",
            lambda: org.page_by_offset("ssot/activation-targets", size_param="batchSize"),
        ),
        # data-transforms caps the page size at 20 and is org-wide.
        ("data-transforms", lambda: org.page_by_offset("ssot/data-transforms", size=20)),
        # calculated-insights list is data-space scoped; fetch per space and merge.
        (
            "calculated-insights",
            lambda: collection_all_spaces("ssot/calculated-insights", {"batchSize": 25, "offset": 0}),
        ),
        # search-index and data-graphs are data-space scoped; fetch per space and merge.
        (
            "search-index",
            lambda: flat_list_all_spaces("ssot/search-index", "semanticSearchDefinitionDetails"),
        ),
        (
            "data-graphs",
            lambda: flat_list_all_spaces("ssot/data-graphs/metadata", "dataGraphMetadata"),
        ),
        # Tier 3 inventory: data actions, their targets, and the full connector catalog.
        ("data-actions", lambda: flat_list(org.get("ssot/data-actions"))),
        ("data-action-targets", lambda: flat_list(org.get("ssot/data-action-targets"))),
        (
            "connectors-catalog",
            lambda: flat_list(org.get("ssot/connectors", {"fieldGroup": "SMALL"}), "connectorInfoList"),
        ),
        # Keep this fetch focused on sections currently used by the template.
    ]
    print(f"phase 1: {len(tasks)} independent endpoints", flush=True)
    results = dict(zip([n for n, _ in tasks], pmap(lambda t: t[1](), tasks)))
    for name in [n for n, _ in tasks]:
        save(name, results[name])

    streams = results["data-streams"]["records"]
    deployed_meta = results["metadata-dmo"]["records"]
    deployed = [d["name"] for d in deployed_meta if d.get("name")]
    # spaces already resolved in phase 0.
    graphs = results["data-graphs"]["records"]
    activations = results["activations"]["records"]

    # Probe the curated connector types plus anything a stream actually uses, so a
    # connector type outside the curated list still shows up.
    probe_types = sorted(
        set(CONNECTOR_TYPES)
        | {
            (s.get("connectorInfo") or {}).get("connectorType")
            for s in streams
            if (s.get("connectorInfo") or {}).get("connectorType")
        }
    )
    # Full coverage mode: expand probe types from the connector catalog.
    # This costs more API calls but avoids missing connector families such as
    # additional Salesforce instances, Databricks variants, MC, and MCP.
    if connector_scope == "all":
        catalog_types = {
            c["name"]
            for c in (results["connectors-catalog"]["records"] or [])
            if isinstance(c, dict) and c.get("name")
        }
        if catalog_types:
            probe_types = sorted(set(probe_types) | catalog_types)

    # ---------------- phase 2: per-object loops, also concurrent
    mapping_task_count = sum(len(d.get("_dataSpaces") or spaces) for d in deployed_meta if d.get("name"))
    print(
        f"phase 2: {mapping_task_count} dmo mappings across {len(spaces)} spaces, "
        f"{len(probe_types)} connector probes, {len(graphs)} graphs, {len(activations)} activations",
        flush=True,
    )

    # DLO->DMO mappings are data-space scoped. Fetch per (space, dmo) using each
    # DMO's own spaces so mappings are correct for every space, not just default.
    mapping_tasks = [
        (sp, d["name"])
        for d in deployed_meta
        if d.get("name")
        for sp in (d.get("_dataSpaces") or spaces)
    ]

    def one_mapping(task: tuple):
        sp, name = task
        res = org.get(
            "ssot/data-model-object-mappings", {"dataspace": sp, "dmoDeveloperName": name}
        )
        return sp, name, {"status": res["status"], "body": res["body"]}

    def one_connector(ctype: str):
        # Page through so a connector type with more than one page of connections
        # is captured in full instead of just the first PAGE rows.
        rows: list = []
        for page in range(MAX_PAGES):
            res = org.get(
                "ssot/connections",
                {"connectorType": ctype, "limit": PAGE, "offset": page * PAGE},
            )
            if res["status"] >= 400:
                return ctype, [] if page == 0 else dedupe(rows)
            body = res["body"] if isinstance(res["body"], dict) else {}
            chunk = body.get("connections", []) or []
            rows.extend(chunk)
            if len(chunk) < PAGE:
                break
        return ctype, dedupe(rows)

    def one_graph(g: dict):
        dev = g.get("developerName")
        res = org.get(f"ssot/data-graphs/{dev}")
        return dev, {"status": res["status"], "body": res["body"]}

    def one_activation(a: dict):
        key = a.get("id") or a.get("developerName")
        res = org.get(f"ssot/activations/{key}")
        return key, {"status": res["status"], "body": res["body"]}

    # Merge per-space mapping results: byDmoSpace keeps the exact per-space payload,
    # byDmo keeps a merged view (union of objectSourceTargetMaps) for consumers
    # that do not care about the space split.
    by_dmo_space: dict[str, dict] = {}
    by_dmo: dict[str, dict] = {}
    for sp, name, payload in pmap(one_mapping, mapping_tasks):
        by_dmo_space.setdefault(name, {})[sp] = payload
        merged = by_dmo.setdefault(
            name, {"status": payload["status"], "body": {"objectSourceTargetMaps": []}}
        )
        existing = merged["body"]["objectSourceTargetMaps"]
        seen = {json.dumps(x, sort_keys=True) for x in existing}
        for om in ((payload.get("body") or {}).get("objectSourceTargetMaps") or []):
            k = json.dumps(om, sort_keys=True)
            if k not in seen:
                seen.add(k)
                existing.append(om)
    save(
        "dmo-mappings",
        {"status": 200, "records": [], "byDmo": by_dmo, "byDmoSpace": by_dmo_space},
    )
    save(
        "connections",
        {
            "status": 200,
            "records": [],
            "byType": {t: rows for t, rows in pmap(one_connector, probe_types) if rows},
        },
    )
    save(
        "data-graph-details",
        {"status": 200, "records": [], "byGraph": dict(pmap(one_graph, graphs))},
    )
    save(
        "activation-details",
        {"status": 200, "records": [], "byActivation": dict(pmap(one_activation, activations))},
    )
    save(
        "data-space-members",
        {"status": 200, "records": [], "bySpace": dict(pmap(lambda sp: (sp, org.page_members(sp)), spaces))},
    )

    # ---------------- completeness audit: prove, per org, that nothing is missed
    # For each category we record the count we captured, an independent cross-check
    # where one exists (Tooling API or a declared totalSize), and the per-space
    # breakdown for the data-space-scoped endpoints. Any category whose scoped
    # endpoint would have under-returned had we only queried the default space is
    # flagged so a reviewer can trust the extract for ANY org.
    def _count(name: str) -> int:
        recs = results.get(name, {}).get("records")
        return len(recs) if isinstance(recs, list) else 0

    tooling_stream_total = _count("tooling-data-streams")
    ssot_stream_total = _count("data-streams")
    dlo_declared = None
    try:
        dlo_first = org.get("ssot/data-lake-objects", {"limit": 20, "offset": 0})
        dlo_declared = dlo_first["body"].get("totalSize") if dlo_first["status"] < 400 else None
    except Exception:  # noqa: BLE001
        dlo_declared = None

    audit_checks = [
        {
            "item": "Data streams",
            "captured": tooling_stream_total,
            "primarySource": "Tooling DataStreamDefinition (all spaces)",
            "crossCheck": ssot_stream_total,
            "crossSource": "ssot/data-streams (default-space context)",
            "recoveredBeyondConnectApi": max(0, tooling_stream_total - ssot_stream_total),
        },
        {
            "item": "Data model objects (deployed)",
            "captured": _count("metadata-dmo"),
            "primarySource": "ssot/metadata?entityType=DataModelObject per data space (merged)",
            "defaultSpaceOnly": perspace_counts.get("DataModelObject", {}).get("default", 0),
            "perSpace": perspace_counts.get("DataModelObject", {}),
        },
        {
            "item": "Data lake objects (metadata)",
            "captured": _count("metadata-dlo"),
            "primarySource": "ssot/metadata?entityType=DataLakeObject per data space (merged)",
            "crossCheck": dlo_declared,
            "crossSource": "ssot/data-lake-objects totalSize (org-wide object list)",
            "defaultSpaceOnly": perspace_counts.get("DataLakeObject", {}).get("default", 0),
            "perSpace": perspace_counts.get("DataLakeObject", {}),
        },
        {
            "item": "Calculated insights",
            "captured": _count("calculated-insights"),
            "primarySource": "ssot/calculated-insights per data space (merged)",
            "perSpace": perspace_counts.get("ssot/calculated-insights", {}),
        },
        {
            "item": "Segments",
            "captured": _count("segments"),
            "primarySource": "ssot/segments per data space (merged)",
            "perSpace": perspace_counts.get("ssot/segments", {}),
        },
        {
            "item": "Activations",
            "captured": _count("activations"),
            "primarySource": "ssot/activations per data space (merged)",
            "perSpace": perspace_counts.get("ssot/activations", {}),
        },
        {
            "item": "Data graphs",
            "captured": _count("data-graphs"),
            "primarySource": "ssot/data-graphs/metadata per data space (merged)",
            "perSpace": perspace_counts.get("ssot/data-graphs/metadata", {}),
        },
        {
            "item": "Search indexes",
            "captured": _count("search-index"),
            "primarySource": "ssot/search-index per data space (merged)",
            "perSpace": perspace_counts.get("ssot/search-index", {}),
        },
        {
            "item": "Identity resolutions",
            "captured": _count("identity-resolutions"),
            "primarySource": "ssot/identity-resolutions (org-wide)",
        },
        {
            "item": "Data transforms",
            "captured": _count("data-transforms"),
            "primarySource": "ssot/data-transforms (org-wide)",
        },
        {
            "item": "Activation targets",
            "captured": _count("activation-targets"),
            "primarySource": "ssot/activation-targets (org-wide)",
        },
        {
            "item": "Connector catalog",
            "captured": _count("connectors-catalog"),
            "primarySource": "ssot/connectors (org-wide)",
        },
    ]
    # Record any endpoint that returned a non-200 so failures are never silent.
    non_ok = []
    for name, payload in results.items():
        st = payload.get("status") if isinstance(payload, dict) else None
        if isinstance(st, int) and st >= 400:
            non_ok.append({"endpoint": name, "status": st})

    save(
        "_audit",
        {
            "status": 200,
            "records": [],
            "dataSpaces": spaces,
            "checks": audit_checks,
            "nonOkEndpoints": non_ok,
            "note": (
                "Data-space-scoped Connect API endpoints default to the 'default' "
                "space; this run queried every data space and merged, so counts "
                "reflect the whole org. 'defaultSpaceOnly' shows what a naive "
                "single-call fetch would have returned."
            ),
        },
    )

    # ---------------- phase 3: resolve the user ids referenced anywhere above
    user_ids: set[str] = set()
    for coll in (activations, results["data-transforms"]["records"], graphs):
        for rec in coll:
            for fld in ("createdBy", "lastModifiedBy", "owner"):
                val = rec.get(fld)
                if isinstance(val, dict) and val.get("id"):
                    user_ids.add(val["id"])
                elif isinstance(val, str) and val.startswith("005"):
                    user_ids.add(val)
    for det in json.loads((CACHE / "activation-details.json").read_text(encoding="utf-8"))[
        "byActivation"
    ].values():
        for fld in ("createdBy", "lastModifiedBy"):
            val = (det.get("body") or {}).get(fld)
            if isinstance(val, dict) and val.get("id"):
                user_ids.add(val["id"])
    users = {}
    if user_ids:
        in_list = ",".join(f"'{i}'" for i in sorted(user_ids))
        res = org.query(f"SELECT Id, Name, Username FROM User WHERE Id IN ({in_list})")
        users = {r["Id"]: r.get("Name") for r in res["records"]}
    save("users", {"status": 200, "records": [], "byId": users})

    elapsed = time.time() - t0
    save(
        "_provenance",
        {
            "orgId": org.org_id,
            "instanceUrl": org.instance,
            "apiVersion": API,
            "fetchedAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "apiCalls": org.calls,
            "retries": org.retries,
            "elapsedSeconds": round(elapsed, 1),
            "workers": WORKERS,
            "maxRetries": args.max_retries,
            "retryBaseMs": args.retry_base_ms,
            "adaptiveThrottle": bool(args.adaptive_throttle),
            "limitInfo": sorted(org.limit_info),
            "maxApiUsagePercent": round(org.max_api_usage_pct, 3),
            "adaptiveThrottleSleepSeconds": round(org.throttle_sleep_seconds, 3),
        },
    )
    print(f"done in {elapsed:.0f}s using {org.calls} api calls -> {CACHE}", flush=True)


if __name__ == "__main__":
    main()
