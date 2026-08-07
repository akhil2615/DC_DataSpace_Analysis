"""Fetch every Data Cloud metadata endpoint needed for the analysis record into a JSON cache.

One command, one pass. Auth comes from the Salesforce CLI default org.
The fetcher first tries `sf org display --json`; if the CLI redacts tokens, it
falls back to `sf org display --verbose --json` to retrieve an access token.
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
    sf_cli = resolve_sf_cli()
    target_args = ["--target-org", target_org] if target_org else []

    def run_and_parse(args: list[str], label: str) -> dict:
        out = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        stdout = (out.stdout or "").strip()
        stderr = (out.stderr or "").strip()
        if out.returncode != 0:
            raise RuntimeError(
                f"Salesforce CLI auth check failed for '{label}'. "
                f"exit={out.returncode} stderr={stderr or '<empty>'} stdout={stdout or '<empty>'}"
            )
        if not stdout:
            raise RuntimeError(
                f"Salesforce CLI returned empty stdout for '{label}'. "
                f"stderr={stderr or '<empty>'}"
            )
        try:
            body = json.loads(stdout)
        except Exception as exc:
            raise RuntimeError(
                f"Unable to parse JSON from '{label}'. "
                f"stdout={stdout[:500]} stderr={stderr[:500]}"
            ) from exc
        return body.get("result") or {}

    # Newer sf versions can redact accessToken on plain display output.
    res = run_and_parse(
        [sf_cli, "org", "display", "--json", *target_args],
        "sf org display --json",
    )
    token = (res.get("accessToken") or "").strip()
    if not token or token.startswith("[REDACTED]"):
        res_verbose = run_and_parse(
            [sf_cli, "org", "display", "--verbose", "--json", *target_args],
            "sf org display --verbose --json",
        )
        token = (res_verbose.get("accessToken") or "").strip()
        # Keep the richer payload from verbose if available.
        if res_verbose:
            res = res_verbose

    if not token or token.startswith("[REDACTED]"):
        raise RuntimeError(
            "Salesforce CLI did not return a usable access token. "
            "Try re-login with 'sf org login web --alias <alias>' and re-run. "
            "If your CLI still redacts tokens, set SF_TEMP_SHOW_SECRETS=true for this shell "
            "or update Salesforce CLI to a version that supports token retrieval commands."
        )

    instance = (res.get("instanceUrl") or "").rstrip("/")
    org_id = res.get("id") or ""
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
        (
            "metadata-dmo",
            lambda: flat_list(org.get("ssot/metadata", {"entityType": "DataModelObject"}), "metadata"),
        ),
        (
            "metadata-dlo",
            lambda: flat_list(org.get("ssot/metadata", {"entityType": "DataLakeObject"}), "metadata"),
        ),
        (
            "metadata-ci",
            lambda: flat_list(org.get("ssot/metadata", {"entityType": "CalculatedInsight"}), "metadata"),
        ),
        # identity-resolutions and search-index take no paging parameters at all.
        ("identity-resolutions", lambda: flat_list(org.get("ssot/identity-resolutions"))),
        ("segments", lambda: org.page_by_offset("ssot/segments", size_param="batchSize")),
        ("activations", lambda: org.page_by_offset("ssot/activations", size_param="batchSize")),
        (
            "activation-targets",
            lambda: org.page_by_offset("ssot/activation-targets", size_param="batchSize"),
        ),
        ("data-spaces", lambda: org.page_by_offset("ssot/data-spaces", size=4999)),
        # data-transforms caps the page size at 20.
        ("data-transforms", lambda: org.page_by_offset("ssot/data-transforms", size=20)),
        (
            "calculated-insights",
            lambda: org.page_by_collection("ssot/calculated-insights", {"batchSize": 25, "offset": 0}),
        ),
        (
            "search-index",
            lambda: flat_list(org.get("ssot/search-index"), "semanticSearchDefinitionDetails"),
        ),
        (
            "data-graphs",
            lambda: flat_list(org.get("ssot/data-graphs/metadata"), "dataGraphMetadata"),
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
    deployed = [d["name"] for d in results["metadata-dmo"]["records"] if d.get("name")]
    spaces = [s["name"] for s in results["data-spaces"]["records"] if s.get("name")]
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
    print(
        f"phase 2: {len(deployed)} dmo mappings, {len(probe_types)} connector probes, "
        f"{len(graphs)} graphs, {len(activations)} activations",
        flush=True,
    )

    def one_mapping(name: str):
        res = org.get("ssot/data-model-object-mappings", {"dmoDeveloperName": name})
        return name, {"status": res["status"], "body": res["body"]}

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

    save(
        "dmo-mappings",
        {"status": 200, "records": [], "byDmo": dict(pmap(one_mapping, deployed))},
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
