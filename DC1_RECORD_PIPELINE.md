# DC1 Data Space Analysis Record — automated fill pipeline

**Date:** 2026-07-28
**Org used for the worked example:** `00DHu00000x1SEKMA2` (`sf` CLI alias `dc-new`, `https://storm-386d1ff7bbaaf0.my.salesforce.com`), Data Cloud REST API **v63.0**
**Scope:** read-only. Every request is a GET. Nothing is written to the org, and the Word template is never modified.

---

## 1. What this does

The DC1 Data Space Analysis Record is a 52-table Word template used to document one Data Cloud data space before a migration. Filling it by hand means hours of clicking through Data Cloud setup and copying API names.

This pipeline reads the org over the Data Cloud REST API, caches every response as JSON, and writes a filled copy of the template. It populates **24 of the template's 52 tables**, which is every table that has a factual source in the API, and it adds three appendices: where each number came from, what a human still has to supply, and which facts in the org look like they need a second opinion.

Three things make the output safe to circulate:

1. **No invented values.** Every populated cell traces to a cached endpoint response. Where the API has no field for a column, the cell says `NOT AVAILABLE FROM API` instead of being filled with a plausible value. Where a value is derived by joining two endpoints, the join is stated in Appendix A.
2. **Nothing is decided for you.** Migration decisions are left blank, and every blank is listed in Appendix B with where the answer comes from and whether it needs a second reviewer.
3. **Findings are separated from facts.** Appendix C lists things worth a human look — a failing activation, a segment with no destination, a ruleset that never runs — as observations. No object is marked in or out of scope on their strength.

---

## 2. How it authenticates

The pipeline runs as **the logged-in Salesforce CLI user**. It calls `sf org display --json`, takes the access token, and issues plain HTTPS GETs to `https://<instance>/services/data/v63.0/ssot/...`.

The Data Cloud **MCP server is not used**. This is deliberate: the MCP server maintains its own auth (which is how an earlier version of this work ended up reading a different org than the CLI pointed at), it times out at 60 seconds per tool call, and it cannot issue the concurrent requests this pipeline depends on. Two consequences worth knowing:

- Whichever org the CLI defaults to is the org that gets documented. Check with `sf org display --json` before running.
- The document only ever contains what that user can see. A user without Data Cloud read access gets empty inventories, not an error.

---

## 3. Prerequisites

| Requirement | Notes |
|---|---|
| Salesforce CLI (`sf`) | Authenticated to the target org and set as the default. |
| Python 3.10+ | Uses only the standard library for HTTP (`urllib`), so no `requests` install is needed. |
| `python-docx` | `pip install python-docx`. Needed by the fill and audit steps only. |
| Data Cloud permissions | Read access to Data Cloud setup, plus read on the Tooling object `MktDataModelObject` and the `User` object. |
| The template file | Default path is `C:\Users\cakhil\Downloads\DC1_Data_Space_Analysis_Record_TEMPLATE.docx`. Change `TEMPLATE` at the top of `scripts/fill_dc1_live.py` if it lives elsewhere. |

---

## 4. Running it

Two commands from the repo root. The fetch writes JSON into `.dc1-cache/`; the fill reads that cache and writes the Word file.

```bash
# 1. Confirm which org you are about to document
sf org display --json

# 2. Pull everything  (~80 s, ~146 API calls)
python -u scripts/dc1_fetch_live.py --clean-cache --workers 8 --max-retries 4 --retry-base-ms 400

# 3. Write one data-space document  (~5 s, no API calls)
python -u scripts/fill_dc1_live.py --space default
```

For an org with many spaces, run the fill step once per space name (same cache, no extra API calls), for example:

```bash
python -u scripts/fill_dc1_live.py --space default
python -u scripts/fill_dc1_live.py --space marketing
python -u scripts/fill_dc1_live.py --space loyalty
```

Output lands next to the template as:

```
DC1_Data_Space_Analysis_Record_default_LIVE_<orgId>_<yyyymmdd>.docx
```

Two helpers for checking the result:

```bash
python scripts/dc1_cache_summary.py          # record count and HTTP status per cached endpoint
python scripts/dc1_audit.py "<output.docx>"  # per-column filled / blank / not-available counts
```

One optional flag. By default the pipeline probes a curated list of connector types plus every type it saw on a data stream, because `/ssot/connections` has no list-all form. If you suspect a configured connection of an unusual type with no stream attached, run the exhaustive version, which probes all 94 connector types the platform supports:

```bash
python -u scripts/dc1_fetch_live.py --all-connectors
```

For production batch execution (fetch once + generate docs for all spaces + run manifest):

```bash
python -u scripts/dc1_run.py --clean-cache --output-dir runs/latest/docs
```

`.dc1-cache/` is in `.gitignore`. It is a full metadata dump of the org, so keep it out of commits and off shared drives.

---

## 5. Timing, and what it costs on a fresh org

Measured on the worked example org from an empty cache, three consecutive cold runs: **79–87 seconds, ~146 API calls**. The fill step adds about 5 seconds and makes no calls.

Where the calls go:

| Group | Calls | Note |
|---|---|---|
| Single-shot inventories | 13 | Streams, segments, activations, targets, data spaces, transforms, CIs, search index, graphs, IR, and three metadata calls |
| DMO field catalogue | 6 | `/ssot/data-model-objects`, 200 per page, fired concurrently |
| DLO inventory | 5 | Caps at 20 per page whatever you ask for |
| DLO-to-DMO mappings | 95 | One per deployed DMO. The endpoint has no bulk form |
| Connector probes | 17 | One per connector type |
| Activation detail | 3 | One per activation, for the payload schema |
| Graph detail, members, users, Tooling | 7 | |

**Estimating another org.** Wall time is not linear in org size, because everything independent runs concurrently at 8 workers. Roughly:

```
seconds ≈ 45  (fixed: the 1090-object CIM catalogue and the single-shot lists)
        + deployed_DMOs / 8 × 1.2   (mapping calls)
        + DLOs / 20 / 8 × 2          (DLO pages)
```

So the example org (95 DMOs, 73 DLOs) lands at about 80 seconds, and an org three times the size lands at about two minutes rather than four. The catalogue is a fixed cost: it returns the whole CIM model, roughly 1090 objects, in every org.

**Human time is the real cost.** The machine finishes in under two minutes; Appendix B lists 46 items that a person has to supply, 32 of which are marked as needing a second reviewer. Those are workshop and policy inputs — migration dispositions, PII classification, consent basis, destination facts, rebuild paths — so a realistic end-to-end for one data space is a couple of hours of preparation plus the workshops the programme already runs. The pipeline's contribution is that nobody spends that time transcribing API names, and the workshop starts from a document where every factual column is already filled and every gap is named.

---

## 6. What lands in each section

Row counts are from the worked example org, to give a sense of scale.

| Template section | Rows | Source | Columns filled from the API |
|---|---|---|---|
| 0.1 Data space object | 1 | `ssot/data-spaces` | Label, API name, default or custom, plus id and status in the note |
| 0.2 Data space scope | 74 | `ssot/data-spaces/{name}/members` joined to `ssot/data-lake-objects` | Scoped object, API name, object type, data space filter |
| 0.3 Extract provenance | 9 | The run itself | How extracted, extract date, evidence file per object class |
| 1.3 Object reconciliation | 11 | All inventories | Extracted, Documented, Reconciles? |
| 2.1 Stream inventory | 52 | `ssot/data-streams?includeMappings=true` | Name, API name, source system, connector type, refresh cadence, row volume, incremental key, last successful run |
| 2.2 Source connections | 22 | `ssot/connections?connectorType=...` | Source, connection API name, connector type as its UI label, raw type and business units in the note |
| 3.1 DLO inventory | 73 | `ssot/data-lake-objects`, `ssot/metadata`, stream mappings | Name, API name, category, field count, record count, primary key, formula fields |
| 4.1 DMO inventory | 95 | `ssot/metadata`, `ssot/data-model-objects`, `ssot/data-model-object-mappings`, Tooling `MktDataModelObject` | Name, API name, creation type, subject area, source DLOs, mapped and unmapped field counts |
| 4.2 DMO relationships | 153 | `ssot/metadata`, data graph tree, segment criteria | From, To, cardinality, and which graph or segment uses the relationship |
| 5.1a Ruleset header | 3 rulesets | `ssot/identity-resolutions` | Ruleset, DMO, individuals in, unified out, match rate, last run, status, automatic, matched and known and anonymous profiles |
| 5.1a Match rules | 3 | `ssot/identity-resolutions` | Order, rule name, match method, fields matched, normalisation and fuzzy settings |
| 5.1b Reconciliation | 10 | `ssot/identity-resolutions` | Attribute group, method, source priority order |
| 6.1 Calculated insights | 2 | `ssot/calculated-insights`, `ssot/metadata` | Name, API name, purpose, source DMOs, refresh schedule, dependent segments, and measure/dimension counts as complexity evidence |
| 6.2 Data graphs | 1 | `ssot/data-graphs/metadata`, `ssot/data-graphs/{devName}` | Name, API name, DMOs included, relationships, zero-copy flag, refresh mode |
| 7.1 Segment inventory | 2 | `ssot/segments` joined to `ssot/activations` | Name, API name, segment-on entity, population, refresh cadence, publish schedule, destinations |
| 7.2 Segment logic | 2 | `ssot/segments` criteria JSON | API name, filter logic, exclusion criteria, CI and graph dependencies |
| 7.3 Segment counts | 2 cells | `ssot/segments` | Live segment count, and how many segments depend on a CI |
| 8.1a Activations | 3 | `ssot/activations`, `ssot/activation-targets` | Name, API name, destination and platform, target id, segment activated |
| 8.1b Payload | 3 | `ssot/activations/{id}`, SOQL `User` | Attribute count and list, cadence, record creator |
| 11.3 Search indexes | 1 | `ssot/search-index` | Name, API name, index type, source DMO, embedded fields, embedding model |
| 12.1 Test baselines | 10 | All inventories | Source value per test measure, as measured at extract time |
| 12.6 Evidence pack | 2 | The cache | Filenames and extract date of the raw metadata extract |

The cover block on page 1 also gets the data space id, label and today's date.

### Derived values, and how they are derived

These are the parts a reviewer is most likely to question, so each one is also stated in Appendix A of the document itself.

- **DLO record counts** come from the row volume of the stream feeding that DLO, matched on `dataLakeObjectInfo.name`. DLOs with no stream (identity resolution output, segment membership tables) correctly show no count.
- **DLO formula fields** come from the stream mappings. Every stream carries three platform-generated formula targets, `DataSource`, `DataSourceObject` and `cdp_sys_PartitionDate`, which are excluded so the column reports author-defined formulas only. The example org has 16 of those across two streams.
- **DMO mapped field count** is the number of distinct mapped target fields in the mapping response. **Unmapped** is the DMO's total field count from the catalogue minus that. This was cross-checked against the catalogue's own `isMapped` flag on every mapped DMO and matches exactly.
- **DMO subject area** is `RefEntitySubjectArea` from the Tooling object `MktDataModelObject`. Only standard CIM DMOs carry one; custom and platform-generated DMOs have none, so 45 of 95 are filled and the rest are a named human input.
- **Relationship "used by"** matches the from-entity and to-entity pair against the data graph object tree and against the objects referenced in segment criteria. 8 of 153 match. The rest read `none detected`, which means nothing we can see references them, not that they are unused.
- **Zero-copy** is traced from a stream with `dataAccessMode` of `DIRECT_ACCESS`, through its DLO, through the DLO-to-DMO mapping, to the DMO. The example org has one such stream.
- **Segment destinations** are matched on `segment.marketSegmentId` equals `activation.segmentId`, an id join rather than a name match.
- **Activation attributes** are the leaf fields of `activationRecordSchema` on the activation detail, expressed as `object.field`.
- **Owner** in 8.1b is the record creator, resolved from `createdBy.id` through a single SOQL query on `User`. It is labelled "record creator" because that is not necessarily the business owner.

---

## 7. How to read an empty cell

There are three distinct kinds of empty, and the difference matters when you review the document.

| What you see | What it means |
|---|---|
| A value | Came from the API. Appendix A names the endpoint. |
| Blank | A migration decision or a human input. Appendix B lists every one of them with where the answer comes from. |
| `NOT AVAILABLE FROM API` | Factual, but no endpoint exposes it in v63.0. Someone has to measure it or read it from the UI. |
| `none detected` | We looked and found no reference. Not the same as "none exists" — it is bounded by what the API exposes. |

The columns in the `NOT AVAILABLE FROM API` category are stream full-refresh duration, DLO partitioning, identity resolution run duration, CI run duration, re-index duration, and activation PII classification and consent basis. Confirmed absent: there is no run-history endpoint for streams (`/runs`, `/run-history` and `/actions/refresh-status` all 404) and identity resolution detail returns exactly what the list returns.

---

## 8. What is never auto-filled

These sections are left as template text, because nothing in the API can answer them: 1.2 migration disposition · 1.4 top risks · 2.3 historical data and backfill · 3.2 data-quality observations · 4.3 field-level disposition · 4.4 attribute precedence · 4.5 transformations required · 5.2 ruleset target · 5.3 template versus country variant · 8.2 destination detail · 8.3 MC journey dependencies · 9.1 consent model · 9.2 residency · 10.1 consumer inventory · 10.2 real-time personalisation · 11.1 direct-read consumers · 11.2 query and SQL consumers · 11.4 data kit packaging · 12.2 to 12.5.

Two of those have a partial automatic contribution worth knowing about:

- **10.1 consumer inventory.** This remains manual by design. The consumer list crosses systems beyond Data Cloud (agents, CRM readers, external SQL callers), so treating a zero-row API response as “no consumers” would be unsafe.
- **11.4 data kit packaging.** `ssot/data-kits` returns a 500 on this org (`Cannot invoke DataSourceBundleDefinition.getDeveloperName() because bundle is null`), so kit contents must be read from the UI.

One gap is worth flagging to whoever owns the template: the org has **3 batch and 10 streaming data transforms**, and the template has no section for them even though page 1 lists data transforms as a migrated object class. They are cached in `.dc1-cache/data-transforms.json`, and a failing one is reported in Appendix C so it is not silently lost.

---

## 9. Endpoint behaviour worth knowing

Found the hard way, then checked against the v67 OpenAPI spec at `developer.salesforce.com/docs/data/connectapi/references/spec`. Anyone extending the pipeline should read this first — most of these cost real time to discover, and three of them silently produce wrong numbers rather than errors.

| Endpoint | Behaviour |
|---|---|
| `ssot/data-streams` | `?limit=200&includeMappings=true` returns every stream **with** its source-to-DLO field mappings, in one call. Omit `includeMappings` and the `mappings` array comes back empty rather than absent, which reads as "this stream has no mappings". This one parameter removed 52 calls from the pipeline. |
| `ssot/data-lake-objects` | The nastiest one. It ignores `limit` and caps at 20. It reports `totalSize` of 83 where only 73 DLOs are readable. A page mid-sequence can come back short (18 of 20) while the next offset still starts at 20, so stepping offsets by the number of records received re-reads rows. Offsets past the end are clamped rather than empty, so the tail page can arrive twice. Results must be deduplicated. |
| `ssot/data-model-objects` | Returns the entire CIM catalogue, 1090 objects, not the deployed ones, and is the only source of total field counts per DMO. Honours limit/offset up to 200. A single big call is a trap: `limit=1200` took 180 seconds and returned an empty array, while six concurrent pages of 200 take 39 seconds. |
| `ssot/metadata?entityType=DataModelObject` | The deployed subset, 95 objects, and the only source of relationships and primary keys. Its `fields` array holds **mapped fields only**, so unmapped counts need the catalogue. Also accepts `DataLakeObject` and `CalculatedInsight`. Rejects `DataGraph`. |
| `ssot/metadata-entities` | v66+, lists deployed objects, but carries no fields. Not a substitute for either of the two above. |
| `ssot/data-model-object-mappings` | Requires `dmoDeveloperName`, one DMO per call. `dloDeveloperName` alone returns `INVALID_INPUT`, and the Tooling API has no `MktDataLakeMapping` object, so there is no bulk form. These 95 calls are irreducible; they are just run concurrently. |
| `ssot/data-transforms` | Page size caps at **20**, not 200. A single default call looks complete and silently truncates any org with more transforms. |
| `ssot/segments`, `ssot/activations`, `ssot/activation-targets` | Default page size is 20 with a cap of 200, and the parameter is `batchSize`, not `limit`. Same silent-truncation risk. |
| `ssot/data-spaces/{name}/members` | Takes `limit` up to 4999, so one call covers any realistic data space. In the example org it returns 74 members while the DLO list returns 73, so 0.2 is built from members to avoid dropping one. |
| `ssot/calculated-insights` | Records are nested at `collection.items`, not a top-level array, and `offset` is mandatory. The metadata endpoint additionally returns internal `_DAY` / `_MONTH` / `_QUARTER` / `_YEAR` rollup entities per CI, which are excluded from 6.1. |
| `ssot/data-graphs` | Returns 400 `Empty Data Graph Name` with no parameters. Use `ssot/data-graphs/metadata` to list and get the object tree, then `ssot/data-graphs/{devName}` for the refresh configuration. The tree's `developerName` values are real deployed DMO names, which is what makes the 4.2 "used by" join possible. |
| `ssot/connections` | Requires a `connectorType`; there is no list-all. `ssot/connectors` lists the 94 types the platform **supports**, not the ones configured, so probing it wholesale costs 94 calls to find the same 7. The default probe list is the curated set plus every type seen on a stream. |
| `ssot/activations/{id}` | Worth the extra call: returns `activationRecordSchema`, `attributesConfig`, `contactPointsConfig`, `createdBy` and `lastModifiedBy`, none of which are on the list response. |
| `ssot/search-index` | Records are at `semanticSearchDefinitionDetails`. `ssot/search-indexes` and `ssot/retrievers` are both 404; retrievers actually live under `ssot/machine-learning/retrievers`. |
| `ssot/streaming-data-transforms` | 404. Batch and streaming transforms both come from `ssot/data-transforms`, distinguished by a `type` of `BATCH` or `STREAMING`. |
| Tooling API | Only `MktDataModelObject` and `MktDataLakeObject` are queryable. `MktSegment`, `MktCalculatedInsight`, `MktDataStream`, `MktActivation` and `MktIdentityResolution` do not exist, so there is no Tooling route to segment or CI descriptions and owners. |
| API versions | v63 through v67 all respond on this org. The pipeline stays on v63.0 because every response shape here is verified against it; nothing the record needs is v67-only. |

---

## 10. Pointing it at another org or data space

**Another org.** Authenticate and set the default, then re-run. The output filename carries the org id, and Appendix A records it, so documents from different orgs cannot be confused.

```bash
sf org login web --alias other-org --set-default
Remove-Item .dc1-cache -Recurse -Force    # be certain nothing survives from the previous org
python -u scripts/dc1_fetch_live.py
python -u scripts/fill_dc1_live.py --space default
```

**Another data space.** The fetch already loops over every space returned by `ssot/data-spaces`. The fill step documents the first one, which is `default` in the example org. For a multi-space org, drive `space_name` in `fill_dc1_live.py` from a command-line argument and run it once per space. Sections 0.2, 6.1 and 7.1 are the ones that genuinely vary by space; the DMO and relationship inventories are org-wide.

---

## 11. Files

| Path | Role |
|---|---|
| `scripts/dc1_fetch_live.py` | The whole extract, one command. Its module docstring is the reference for endpoint quirks. |
| `scripts/fill_dc1_live.py` | Reads the cache, writes the Word file. All section-to-table mapping, the human input register and the observation rules live here. |
| `scripts/dc1_run.py` | Production orchestrator: one fetch + per-space generation + run manifest and logs under `runs/<timestamp>/`. |
| `scripts/dc1_cache_summary.py` | What is in the cache and whether each call succeeded. |
| `scripts/dc1_audit.py` | Per-column fill statistics for a finished document. |
| `.dc1-cache/` | 28 cached JSON responses plus `_provenance.json`, which records org id, instance, API version, timestamp, call count and elapsed time. Gitignored. |

`scripts/fill_dc1_corrected_fullpass.py` is an earlier snapshot-driven version. It is superseded and its DLO, DMO and relationship counts are wrong; keep it only for reference.

---

## 12. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Every inventory comes back empty or 403 | The CLI token lacks Data Cloud access, or the CLI session expired. Re-run `sf org display --json` and check `connectedStatus`. |
| `dc1_cache_summary.py` shows `status=0` on a row | Network timeout, not an API error. Re-run the fetch; it is idempotent. |
| Documenting the wrong org | The pipeline follows the CLI default org, not the MCP server. Check `sf org display --json` first; the org id is also in the output filename and Appendix A. |
| MCP Data Cloud tools time out at 60 seconds while a fetch runs | The MCP server shells out to `sf org display` and contends with the pipeline for the CLI. Wait for the fetch to finish, then retry the tool. |
| Fewer DLOs than the UI shows | Pagination stopped early, or `totalSize` was trusted. The endpoint caps at 20 per page and overstates `totalSize`; see section 9. |
| More DLOs than the UI shows | Duplicate rows from overlapping offsets. `dedupe()` in the fetcher handles this; check it is still being applied. |
| An inventory is suspiciously round, like exactly 20 rows | A page-size default was hit. Segments, activations, and activation targets use `batchSize`; transforms cap at 20. |
| Row counts in 3.1 are empty for everything | The stream-to-DLO join is not matching. The stream field is `dataLakeObjectInfo.name`, not `dataLakeObject.name`. |
| `dmo-mappings` shows 0 successes | The query parameter must be `dmoDeveloperName`. |
| Formula fields column says N everywhere | `includeMappings=true` was dropped from the stream list call, so `mappings` came back empty rather than missing. |
| The template itself looks modified | It should never be. The fill step opens the template read-only and saves to a new filename. Restore from source control or a backup and re-run. |
