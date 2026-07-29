# DC360-MCP — Salesforce Data Cloud MCP Server for Cursor

An MCP (Model Context Protocol) server that gives [Cursor AI](https://cursor.sh) live access to your
Salesforce Data Cloud (Data 360) org — enabling AI-assisted authoring of formulas, streaming
transforms, calculated insight SQL, segment logic, and ad-hoc troubleshooting queries.

Built on top of the [datacloud-mcp-query](https://github.com/forcedotcom/datacloud-mcp-query)
example by Salesforce, extended with metadata APIs, code generation tools, and a
comprehensive Cursor skill with battle-tested syntax rules.

---

## Table of Contents

- [What It Does](#what-it-does)
- [Cursor Skill (Included)](#cursor-skill-included)
- [Prerequisites](#prerequisites)
- [Setup — Step by Step](#setup--step-by-step)
  - [Step 1: Clone the Repo](#step-1-clone-the-repo)
  - [Step 2: Install Python (if needed)](#step-2-install-python-if-needed)
  - [Step 3: Create Virtual Environment & Install Dependencies](#step-3-create-virtual-environment--install-dependencies)
  - [Step 4: Create a Connected App in Salesforce](#step-4-create-a-connected-app-in-salesforce)
  - [Step 5: Add the MCP Server to Cursor](#step-5-add-the-mcp-server-to-cursor)
  - [Step 6: Install the Cursor Skill](#step-6-install-the-cursor-skill)
  - [Step 7: Activate and Test](#step-7-activate-and-test)
- [Connecting to a Different Org](#connecting-to-a-different-org)
- [Authentication Flow](#authentication-flow)
- [Environment Variables](#environment-variables)
- [Usage Examples](#usage-examples)
- [Syntax Rules Summary](#syntax-rules-summary)
- [Architecture](#architecture)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## What It Does

| Category | Tool | Description |
|----------|------|-------------|
| **Schema Discovery** | `list_data_lake_objects` | Browse all raw DLOs in your org |
| | `describe_data_lake_object` | Inspect DLO fields before writing formulas |
| | `list_data_model_objects` | Browse all harmonised DMOs |
| | `describe_data_model_object` | Inspect DMO fields before writing SQL / segments |
| | `list_calculated_insights` | See all existing calculated insights |
| | `describe_calculated_insight` | Understand an insight's dimensions & measures |
| **Code Generation** | `generate_formula` | Write Data Stream formula field expressions |
| | `generate_streaming_transform` | Write streaming transform SQL |
| | `generate_calculated_insight_sql` | Write valid Calculated Insight SQL |
| | `generate_segment_logic` | Build segment filter expressions with container logic |
| **Query & Troubleshoot** | `query` | Execute SQL in the Data Cloud query engine |
| | `list_tables` | Quick table inventory via pg_catalog |
| | `describe_table` | Column list via pg_catalog |
| | `troubleshoot_data` | Diagnose data quality issues with targeted queries |
| **Documentation** | `export_dlo_fields` | Export DLO fields for documentation |
| | `export_dmo_fields` | Export DMO fields with primary key info |
| | `export_dlo_to_dmo_mapping` | Export DLO-to-DMO field mapping template |
| | `export_dmo_relationships` | Export DMO-to-DMO relationship map |
| **Utility** | `debug_auth` | Show resolved SF and DC instance URLs |
| | `datacloud_help` | Answer any conceptual Data Cloud question |

---

## Cursor Skill (Included)

The `.cursor/skills/salesforce-datacloud/` directory contains a comprehensive skill
that teaches Cursor the correct syntax for every Data Cloud feature:

- **Formula fields** — `sourceField['Label']` syntax, AND/OR as infix operators,
  double-quoted strings, supported functions (IF, PROPER, NOW, etc.)
- **Streaming transforms** — `DLOName__dll.Field__c` dot notation, explicit AS aliases,
  single-quoted strings, no comments, SUBSTRING instead of LEFT, ISNULL/ISNOTNULL
- **Calculated Insights** — `__c` suffix aliases, GROUP BY aliases, CDPHour(),
  NTILE/RANK/DENSE_RANK, inline views, standard join paths
- **Query Editor** — table aliases, ROW_NUMBER(), date arithmetic, JOIN ON with parens
- **Segments** — all 6 segment types, operators by data type, containers, aggregation,
  container paths, nested operators, best practices
- **Documentation exports** — DLO/DMO field lists, DLO-to-DMO mapping, DMO relationships

All syntax rules were validated against a real Data Cloud org and corrected iteratively.

---

## Prerequisites

- **Python 3.10+** — [download here](https://www.python.org/downloads/) if not installed
- **Cursor IDE** — [download here](https://cursor.sh)
- A **Salesforce org** with Data Cloud (Data 360) provisioned
- **System Administrator** access to create a Connected App

---

## Setup — Step by Step

### Step 1: Clone the Repo

Open a terminal (PowerShell on Windows, Terminal on macOS/Linux):

```bash
git clone https://github.com/akhil2615/DC360-MCP.git
cd DC360-MCP
```

### Step 2: Install Python (if needed)

Check if Python is installed (the correct command depends on your install method):

```bash
# Try whichever works on your system:
python --version
python3 --version
python3 -V
python -V
```

You need **Python 3.10 or higher**. If not installed:
- **Windows**: Download from [python.org](https://www.python.org/downloads/).
  During installation, check **"Add Python to PATH"**. Restart your terminal after installing.
- **macOS**: `brew install python` or download from python.org
- **Linux**: `sudo apt install python3 python3-venv` (Ubuntu/Debian)

### Step 3: Create Virtual Environment & Install Dependencies

```bash
# Create virtual environment
# Use whichever python command worked in Step 2:
python -m venv .venv
# or
python3 -m venv .venv

# Activate it
# Windows (PowerShell):
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

You should see `(.venv)` at the start of your prompt, confirming the virtual environment is active.

> **If you get a permissions error on Windows** ("running scripts is disabled"), run this first:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

**Note the full path to Python** — you'll need it for the Cursor config:

```bash
# Windows
(Get-Command python).Source
# Example output: C:\Users\you\DC360-MCP\.venv\Scripts\python.exe

# macOS / Linux
which python
# Example output: /Users/you/DC360-MCP/.venv/bin/python
```

### Step 4: Create a Connected App in Salesforce

This is a **one-time setup per org**. Follow the detailed guide in
[CONNECTED_APP_SETUP.md](CONNECTED_APP_SETUP.md).

**Quick summary:**

1. In your Salesforce org, go to **Setup → App Manager → New Connected App**
2. Name it `Data Cloud MCP`, enable OAuth, add these scopes:
   - `Access and manage your data (api)`
   - `Access and manage Data Cloud Ingestion API data (cdp_ingest_api)`
   - `Access and manage Data Cloud profile data (cdp_profile_api)`
   - `Perform ANSI SQL queries on Data Cloud data (cdp_query_api)`
3. Set Callback URL to `http://localhost:55556/Callback`
4. Enable PKCE
5. Save → wait 2-10 minutes for it to activate
6. Click **Manage Consumer Details** → copy the **Consumer Key** and **Consumer Secret**
7. Assign the **Data Cloud Admin** permission set to your user

### Step 5: Add the MCP Server to Cursor

1. Open **Cursor IDE**
2. Go to **Cursor Settings** (gear icon top-right, or `Ctrl + ,`)
3. Click **MCP** in the left sidebar
4. Click **Edit Config** — this opens `mcp.json` in your editor

> **Where this file lives.** "Edit Config" opens Cursor's user-level
> `mcp.json`, which is **outside this repo**:
> - **Windows**: `%USERPROFILE%\.cursor\mcp.json`
> - **macOS / Linux**: `~/.cursor/mcp.json`
>
> Because you're about to paste your Salesforce **Consumer Secret** into
> this file, double-check it isn't being backed up or synced anywhere
> public (OneDrive, iCloud, a dotfiles repo, Time Machine to a shared
> drive, etc.). Treat the file like a credential.

5. Add the `datacloud` entry inside `mcpServers`:

**Windows:**
```json
{
  "mcpServers": {
    "datacloud": {
      "command": "C:\\Users\\YOUR_USERNAME\\DC360-MCP\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\YOUR_USERNAME\\DC360-MCP\\server.py"],
      "env": {
        "SF_CLIENT_ID": "paste-your-consumer-key-here",
        "SF_CLIENT_SECRET": "paste-your-consumer-secret-here",
        "SF_LOGIN_URL": "login.salesforce.com",
        "SF_CALLBACK_URL": "http://localhost:55556/Callback"
      },
      "disabled": false,
      "autoApprove": [
        "list_data_lake_objects",
        "list_data_model_objects",
        "list_calculated_insights",
        "list_tables",
        "describe_data_lake_object",
        "describe_data_model_object",
        "describe_calculated_insight",
        "describe_table",
        "debug_auth"
      ]
    }
  }
}
```

**macOS / Linux:**
```json
{
  "mcpServers": {
    "datacloud": {
      "command": "/Users/YOUR_USERNAME/DC360-MCP/.venv/bin/python",
      "args": ["/Users/YOUR_USERNAME/DC360-MCP/server.py"],
      "env": {
        "SF_CLIENT_ID": "paste-your-consumer-key-here",
        "SF_CLIENT_SECRET": "paste-your-consumer-secret-here",
        "SF_LOGIN_URL": "login.salesforce.com",
        "SF_CALLBACK_URL": "http://localhost:55556/Callback"
      },
      "disabled": false,
      "autoApprove": [
        "list_data_lake_objects",
        "list_data_model_objects",
        "list_calculated_insights",
        "list_tables",
        "describe_data_lake_object",
        "describe_data_model_object",
        "describe_calculated_insight",
        "describe_table",
        "debug_auth"
      ]
    }
  }
}
```

> **Note on `autoApprove`.** This key is a **Cursor-specific** extension —
> it tells Cursor not to ask for confirmation each time the listed read-only
> tools are called. Other MCP clients (Claude Desktop, etc.) ignore the key
> safely; you'll just get a per-call confirmation prompt instead. If you
> prefer to approve every call manually, replace the array with `[]` or
> remove the key.

6. **Replace** `YOUR_USERNAME` with your **operating-system username** (the
   one in your home directory path) — **NOT** your Salesforce username.
   - Windows: run `echo $env:USERNAME` in PowerShell, or look at your user
     folder name under `C:\Users\`
   - macOS / Linux: run `whoami` or `echo $USER`
7. **Paste** your Consumer Key and Consumer Secret from Step 4
8. **Save** the file (`Ctrl + S`)

> **Sandbox orgs**: change `"SF_LOGIN_URL"` to `"test.salesforce.com"`

### Step 6: Install the Cursor Skill

Copy the included skill to your personal Cursor skills folder:

**Windows (PowerShell):**
```powershell
Copy-Item -Recurse .cursor\skills\salesforce-datacloud $env:USERPROFILE\.cursor\skills\ -Force
```

**macOS / Linux:**
```bash
mkdir -p ~/.cursor/skills
cp -r .cursor/skills/salesforce-datacloud ~/.cursor/skills/
```

The skill is automatically discovered by Cursor — no restart needed.

### Step 7: Activate and Test

1. Go to **Cursor Settings → MCP**
2. Find `datacloud` in the list
3. If it shows a red dot, click **Refresh** (↺)
4. Wait for the green dot to appear — this means the server is connected
5. Open a new chat in Cursor and type:

> "List all data lake objects in my Data Cloud org"

6. A **browser window will open** asking you to log in to Salesforce
7. Log in normally — you'll see `Final Status: has_code=True` in the browser
8. Close the browser tab — back in Cursor, you should see the list of DLOs

You're all set!

---

## Connecting to a Different Org

To switch to a different Salesforce org:

1. **Create a Connected App** in the new org (follow [CONNECTED_APP_SETUP.md](CONNECTED_APP_SETUP.md))
2. **Update `mcp.json`** in Cursor with the new credentials:
   ```json
   "SF_CLIENT_ID": "new-consumer-key",
   "SF_CLIENT_SECRET": "new-consumer-secret",
   "SF_LOGIN_URL": "login.salesforce.com"
   ```
3. **Restart the MCP**: Settings → MCP → toggle `datacloud` off → wait 2 seconds → on
4. A new browser window will open for OAuth login to the new org

> **Multiple orgs**: You can add separate entries in `mcp.json` for each org:
> ```json
> {
>   "mcpServers": {
>     "datacloud-prod": {
>       "command": "...",
>       "args": ["..."],
>       "env": {
>         "SF_CLIENT_ID": "prod-key",
>         "SF_CLIENT_SECRET": "prod-secret",
>         "SF_LOGIN_URL": "login.salesforce.com"
>       }
>     },
>     "datacloud-sandbox": {
>       "command": "...",
>       "args": ["..."],
>       "env": {
>         "SF_CLIENT_ID": "sandbox-key",
>         "SF_CLIENT_SECRET": "sandbox-secret",
>         "SF_LOGIN_URL": "test.salesforce.com"
>       }
>     }
>   }
> }
> ```
> Disable the one you're not using to avoid conflicts.

---

## Authentication Flow

The server uses a two-step OAuth flow:

```
Step 1: Browser OAuth2 PKCE
  User logs in via browser → Salesforce returns SF access_token + instance_url
  Scopes: api, cdp_query_api, cdp_profile_api, cdp_ingest_api

Step 2: Data Cloud Token Exchange
  POST {instance_url}/services/a360/token
  grant_type         = urn:salesforce:grant-type:external:cdp
  subject_token      = <SF access_token>
  subject_token_type = urn:ietf:params:oauth:token-type:access_token
  → Returns DC access_token + c360a tenant URL

SF token  → used for Query API (/services/data/v63.0/ssot/query-sql)
DC token  → used for Metadata API ({c360a_url}/api/v1/metadata/)
```

Both tokens auto-refresh after ~110 minutes. If the token expires, the browser
will open again for re-authentication.

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SF_CLIENT_ID` | Yes | — | Connected App Consumer Key |
| `SF_CLIENT_SECRET` | Yes | — | Connected App Consumer Secret |
| `SF_LOGIN_URL` | No | `login.salesforce.com` | `test.salesforce.com` for sandbox |
| `SF_CALLBACK_URL` | No | `http://localhost:55556/Callback` | OAuth redirect URI |
| `DEFAULT_LIST_TABLE_FILTER` | No | `%` | SQL LIKE filter for `list_tables` |

---

## Usage Examples

### Generate a formula field
> "Write a formula for Lead_Home__dll that flags junk emails as Yes/No"

### Write a streaming transform
> "Create a streaming transform for Lead_Home__dll that cleanses email, phone,
> first/last name and classifies IR readiness"

### Write a Calculated Insight
> "Write a CI that calculates lifetime spend per unified individual,
> ranked by total amount"

### Build a segment
> "Create a segment for customers in the US who purchased at least twice in the
> last 90 days with total spend > $500"

### Troubleshoot data
> "Emails are null in UnifiedIndividual after today's CRM data stream run.
> Write queries to investigate."

### Document your data model
> "Export all fields from Lead_Home__dll to a CSV with DMO mappings"

### Ask questions about Data Cloud
> "What is identity resolution and how does it work in Data Cloud?"

---

## Syntax Rules Summary

| Feature | Key syntax rules |
|---------|-----------------|
| Formula fields | `sourceField['Label']` (display name, not API name), AND/OR as infix operators, double-quoted string values |
| Streaming transforms | `DLO__dll.Field__c AS Field__c` (dot notation, explicit aliases), single-quoted strings, no comments, no LEFT() — use SUBSTRING() |
| Calculated Insights | All aliases end with `__c`, GROUP BY uses alias names, no double-quoting, `CDPHour()` for time dimensions |
| Query Editor | Table aliases, `ROW_NUMBER()`, `column + interval '9 hour'`, comments OK, LIMIT 10000 max |
| Segments | Structured Segment Builder steps: Segment On entity, Direct/Related attributes, Containers with aggregation, Container paths |

---

## Architecture

```
DC360-MCP/
├── server.py                   — 20 MCP tools (FastMCP)
├── oauth.py                    — Two-step OAuth: SF PKCE + DC a360 token exchange
├── connect_api_dc_sql.py       — Data Cloud Query API with long-polling & pagination
├── dc_metadata_api.py          — Metadata API (REST primary, SQL fallback)
├── requirements.txt            — Python dependencies
├── .env.example                — Template for credentials
├── CONNECTED_APP_SETUP.md      — Detailed Connected App guide
├── README.md                   — This file
├── samples/                    — Example outputs from the export tools (optional, for reference)
└── .cursor/
    └── skills/
        └── salesforce-datacloud/
            ├── SKILL.md                — Cursor skill: workflows and syntax rules
            └── dc-syntax-reference.md  — Full reference: operators, functions, templates
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| MCP shows red dot / "Not connected" | Restart: toggle off → wait 2 sec → toggle on. If still failing, fully restart Cursor. |
| "Missing required environment variables" | Check that `SF_CLIENT_ID` and `SF_CLIENT_SECRET` are set in `mcp.json` |
| "invalid_client" error | Consumer Key or Secret is wrong — re-copy from Connected App |
| "invalid_grant" error | User doesn't have Data Cloud permission set assigned |
| Browser doesn't open for login | Port 55556 may be blocked — check firewall settings |
| "OAUTH_APPROVAL_ERROR" | Connected App hasn't activated yet — wait 10 minutes after creation |
| "invalid_subject_token" on DC token | Missing `cdp_query_api` or `cdp_profile_api` scope in Connected App |
| Old code still running after file edits | Kill stale Python processes, then restart MCP |
| "URL No Longer Exists" on metadata | Run `debug_auth()` to check the c360a URL is correct |
| Tools not appearing in Cursor | Restart Cursor completely (close + reopen) |

---

## License

Apache 2.0
