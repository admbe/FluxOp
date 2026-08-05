<p align="center">
  <img src="assets/flux-banner.png" alt="Flux Intelligence — governed evidence, analyzed." width="720">
</p>

<h1 align="center">Flux</h1>

<p align="center"><b>Governed evidence, analyzed.</b></p>

<p align="center">
  <a href="https://fluxop.ai"><img src="https://img.shields.io/badge/website-fluxop.ai-0f9d8c" alt="fluxop.ai"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-3776ab?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/react-19-61dafb?logo=react&logoColor=white" alt="React 19">
  <img src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/DuckDB-1.4.5-fff000?logo=duckdb&logoColor=black" alt="DuckDB 1.4.5">
  <img src="https://img.shields.io/badge/Azure-Resource%20Graph%20%C2%B7%20Cost%20Management-0078d4?logo=microsoftazure&logoColor=white" alt="Azure">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/FOCUS-v1.0-5b21b6" alt="FOCUS v1.0">
  <img src="https://img.shields.io/badge/FinOps%20Toolkit-v14-5b21b6" alt="Microsoft FinOps Toolkit v14">
  <a href="docs/FLUX-INTELLIGENCE.md"><img src="https://img.shields.io/badge/Ask%20Flux-19%20governed%20tools-0f9d8c" alt="Ask Flux: 19 governed tools"></a>
  <img src="https://img.shields.io/github/last-commit/admbe/FluxOp?color=555" alt="Last commit">
  <img src="https://img.shields.io/github/languages/top/admbe/FluxOp?color=3178c6" alt="Top language">
</p>

<p align="center">
  <a href="https://fluxop.ai">Website</a> ·
  <a href="#product-scope">Product scope</a> ·
  <a href="#flux-intelligence">Flux Intelligence</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="docs/README.md">Documentation</a> ·
  <a href="#api">API</a>
</p>

---

Flux is a focused Azure inventory and FinOps intelligence workspace.

It synchronizes Azure Resource Graph, Azure Advisor, Cost Management, Azure Monitor, and LogicMonitor coverage into DuckDB, presents the estate through a modern React dashboard, and surfaces explainable opportunities with source lineage.

## Product scope

The active application contains ten areas:

- **Overview** — rich charts for estate shape, regional footprint, actual month-to-date cost, utilization coverage, and opportunities.
- **Inventory** — searchable Azure Resource Graph inventory enriched with cost and VM performance summaries.
- **Changes** — exact consecutive-snapshot inventory diffs with filterable evidence and warming-up-aware median/MAD change-volume detection.
- **Cost anomalies** — governed seasonal anomaly detection, triage, export, and evidence packs.
- **Reports** — native cost, forecast, workload, retirement, allocation, budget, and Azure Policy reporting across four sections.
- **Explore** — an ad-hoc query builder over the governed semantic layer, plus an expert mode that generates validated read-only SQL from a plain-language question.
- **Opportunities** — searchable Azure Advisor recommendations and branded **Flux Signals** findings, with evidence, confidence, actual-cost context, and provenance-aware gross and risk-adjusted value.
- **Right-sizing plan** — planning boards that turn telemetry-backed candidates into reservation and savings-plan purchase decisions, with a decision log.
- **[Flux Intelligence](#flux-intelligence)** — **Ask Flux**, the read-only conversational assistant that answers from governed evidence rather than raw database access.
- **Administration** — Azure tenant/subscription scope, independent source-run status, telemetry coverage, and AI configuration.

The former scenario studio, ad-hoc file ingestion, generated dashboards, and browser-only persistence are not part of v2.

## Technology

- React 19, TypeScript, and Vite
- Recharts and Lucide
- FastAPI and Pydantic
- DuckDB
- App Service Authentication and Microsoft Entra app roles
- Azure Identity for secretless ARG, Advisor, and Cost Management access
- Local Azure PowerShell identity for development access

See the [documentation index](docs/README.md) for all guides — [docs/architecture.md](docs/architecture.md) for the design and extension model and [docs/entra-managed-identity.md](docs/entra-managed-identity.md) for Azure configuration.

## Quick start

Prerequisites:

- Python 3.10+
- Node.js 20+
- PowerShell 7 recommended

Install:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r .\requirements.txt

Set-Location .\frontend
npm install
npm run build
Set-Location ..
```

Run the production-style local host:

```powershell
python .\app.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765).

The launcher builds the frontend when needed:

```powershell
.\start-flux.ps1
```

### Development mode

Run the API:

```powershell
python .\app.py
```

In another terminal:

```powershell
Set-Location .\frontend
npm run dev
```

Open [http://localhost:5173](http://localhost:5173). Vite proxies `/api` to port `8765`.

### Optional demonstration data

To evaluate all chart and enrichment states without an Azure connection:

```powershell
$env:FLUX_DEV_SEED = "true"
python .\app.py
```

Demo rows are inserted only when the DuckDB snapshot table is empty and are marked with demo source lineage.

## Identity model

Flux uses two independent identities:

- **People authenticate with Microsoft Entra ID.** App Service Authentication validates the user and injects the `X-MS-CLIENT-PRINCIPAL` claims payload. Flux maps roles or group IDs to `reader` and `admin`.
- **Flux authenticates to Azure with managed identity.** The application obtains a management token without a client secret and queries ARG, AdvisorResources, and Cost Management for the explicitly configured subscriptions.

Authorization:

| Flux role | Access |
|---|---|
| `reader` | Overview, inventory, opportunities |
| `admin` | All reader access plus integration configuration and synchronization |

Default Entra app-role values are `Flux.Reader` and `Flux.Admin`. Group object IDs can be mapped through environment settings instead.

Local development defaults to a mock administrator. Set `FLUX_AUTH_MODE=entra` only behind correctly configured App Service Authentication; the application then trusts the principal header injected by App Service.

## Flux Intelligence

<img src="assets/flux-robot.svg" alt="" width="72" align="right">

**Flux Intelligence** is the umbrella capability. **Ask Flux** is its
conversational assistant, **Flux Signals** is the deterministic optimization
rule engine, and governed intelligence tools are the bounded report/evidence
APIs behind both experiences.

### Governed evidence, not database access

Most "AI for your data" tools hand a model a database connection and hope the
generated SQL is right. Flux does the opposite: the model has **no** database
connection, Azure credential, Rill endpoint, or arbitrary query interface. It
can call only **19 declared server-side tools**, each of which validates and
bounds its arguments before invoking the same governed services the UI uses.

The practical consequences:

- An answer is reproducible — every number came from a named tool over
  governed data, and the tools are listed with each reply.
- Coverage gaps are stated rather than papered over. If a subscription's cost
  export is missing, the answer says so before it states a total.
- The model cannot be prompted into reading something it was never granted;
  there is no query surface to redirect.

### What you can ask

Ask Flux answers questions across cost, changes, anomalies, optimization,
right-sizing, inventory, governance, and reporting — for example:

- *"What changed in amortized cost this month compared to last?"*
- *"Attribute this month's increase to specific resources."*
- *"Which VMs are the strongest right-sizing candidates?"*
- *"Where are telemetry coverage gaps limiting right-sizing decisions?"*
- *"How much are idle and orphaned resources costing me?"*
- *"What is my tag compliance rate, and where are the gaps?"*
- *"Explain how Flux calculates a cost anomaly."*

### What comes back

A reply is a validated structure, not free text. It can carry a summary,
Markdown, governed Recharts specifications, strict Mermaid diagrams, and — kept
deliberately separate — **retrieved facts**, **interpretation**, **limitations**,
and the **governed sources** invoked. Server-owned action links connect the
evidence back to the relevant Flux page, follow-up questions are offered, and a
per-answer performance breakdown shows where the time went. Every reply also
receives a deterministic 0–100 quality score covering structure, grounding,
partial-coverage disclosure, table validity, follow-up perspective, and summary
completeness.

### Access, retention, and cost

Both experiences — the contextual **Ask Flux** panel and the full-screen
**Intelligence Workspace** — require the existing `Flux.Reader` or `Flux.Admin`
application role. Prompts, validated replies, and the raw final response are
retained in DuckDB for 30 days for administrator quality review; model reasoning
is never retained. Pseudonymous performance and usage metadata is retained for
the same period. Fast and deep-analysis profiles run through a swappable
provider adapter (DeepSeek, OpenRouter, or Azure AI Foundry), with a default USD
10 evaluation budget that stops requests at an estimated USD 8.

See [docs/FLUX-INTELLIGENCE.md](docs/FLUX-INTELLIGENCE.md) for the full tool
catalog, provider configuration, output controls, and known limitations.

## Connect Azure locally

The local provider uses the current Azure PowerShell session.

```powershell
Connect-AzAccount
```

Then:

1. Open **Integrations**.
2. Add the tenant ID if you want Flux to verify it.
3. Add one or more subscription IDs and friendly names.
4. Save.
5. Select **Synchronize now**.

The identity needs permission to read resources and Cost Management data for the selected subscriptions. Sync is paginated and stores inventory, Advisor, and cost observations with independent source-completion state.

## Connect Azure with managed identity

In App Service:

1. Enable a system-assigned or user-assigned managed identity.
2. Grant that identity resource read access and `Microsoft.CostManagement/*/read` on every subscription Flux will query. The deployed custom `FinOps Platform Reader` role provides these permissions.
3. Enable App Service Authentication with Microsoft Entra ID and require authentication.
4. Add and assign `Flux.Reader` and `Flux.Admin` app roles, or configure group-ID mappings.
5. Set:

   ```text
   FLUX_AUTH_MODE=entra
   FLUX_ENTRA_TENANT_ID=<tenant-id>
   FLUX_ENTRA_ADMIN_ASSIGNMENTS=Flux.Admin
   FLUX_ENTRA_READER_ASSIGNMENTS=Flux.Reader
   ```

6. In Flux Integrations, select **App Service managed identity** and synchronize.

For a user-assigned identity, also set `FLUX_MANAGED_IDENTITY_CLIENT_ID` to its client ID. System-assigned identity requires no client ID setting.

See [docs/entra-managed-identity.md](docs/entra-managed-identity.md) for the complete deployment checklist and RBAC commands.

## Continuous delivery

[`azure-pipelines.yml`](azure-pipelines.yml) defines the production delivery path:

1. commits and pull requests targeting `main` trigger validation;
2. Azure Pipelines installs dependencies, type-checks and builds React, and runs the Python tests;
3. production Python dependencies and `frontend/dist` are packaged into a versioned ZIP artifact;
4. successful `main` builds deploy to the Linux App Service through a workload-identity service connection.

The pipeline targets (set these for your environment in the pipeline variables):

| Setting | Value |
|---|---|
| Branch | `main` |
| App Service | your Flux App Service name |
| Resource group | your resource group |
| Azure subscription | the subscription hosting the App Service |
| Deployment environment | e.g. `Flux-Production` |

The service connection should use workload identity federation and have deployment access scoped to the `FluxFinOps` App Service. No publish profile, client secret, or PAT belongs in the repository.

The deployment package vendors portable `manylinux_2_28_x86_64` Python dependencies under `.python_packages/lib/site-packages` so the main container and Linux WebJob hosts use the same compatible artifact. The pipeline maintains `PYTHONPATH=/home/site/wwwroot/.python_packages/lib/site-packages`, configures the production API to enqueue rather than execute sync work, and verifies that the protected health endpoint reaches the Entra sign-in boundary after every production deployment.

Interactive and scheduled Azure synchronizations are persisted in `sync_runs`.
A singleton continuous WebJob claims queued requests and owns collection.
Inventory, Advisor, Flux Intelligence, Azure Policy, and Cost Management have independent
scheduled requests; DuckDB writes remain serialized through the one worker.
An independent retail-price WebJob runs every six hours, discovers newly observed
Advisor target SKU keys, and refreshes each cached Microsoft retail rate daily.
The read-only Flux right-sizing proposal is regenerated from governed evidence
every 72 hours by a daily due-check WebJob, and administrators can force the same
deterministic refresh from Administration. Planners copy the system board before
changing placements or purchase assumptions; scheduled refreshes never overwrite
human boards.
An independent daily-cost WebJob backfills 90 days on first collection, refreshes
only the latest 14 days thereafter, and evaluates finalized daily spend against a
matching-weekday median/MAD baseline. Initial and rolling queries are committed in
bounded, newest-first date windows. Completed windows survive later throttling or
database failures, while a partial initial scope remains retry-eligible rather
than being promoted to rolling refresh. If the Query API persistently fails for a
scope, an automated Cost Details fallback fills checkpointed calendar months,
up to four reports per daily run. It does not lengthen the primary Azure sync.
Every request records child source/scope attempts in `sync_source_runs`, allowing
recovery to skip completed scopes and retry only unfinished work.
If the worker exits, the operating-system sync lease is released and the replacement
worker recovers the unfinished request. Local development uses the same consumer
loop in embedded mode.

Production schedules are:

- Azure inventory and Policy posture daily at 10:00 UTC;
- Flux Intelligence daily at 10:30 UTC;
- Cost Management daily at 11:00 UTC;
- daily Cost Management history and anomaly evaluation at 12:30 UTC;
- idempotent FOCUS cost-export ingestion every six hours;
- Azure Advisor every six hours at `:45`;
- LogicMonitor discovery every six hours at `:10` and Azure Monitor at `:15`;
- incremental LogicMonitor metric collection every 30 minutes in rotating, checkpointed batches;
- Microsoft FinOps Toolkit v14 open data weekly on Sunday at 03:00 UTC.
- Flux right-sizing proposal due-check daily at 04:05 UTC (regenerates after 72 hours).

The canonical implementation backlog and shipped-feature record is
[docs/FEATURE-CHECKLIST.md](docs/FEATURE-CHECKLIST.md).

## API

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/health` | Runtime and database health |
| `GET` | `/api/session` | Current Entra identity, roles, and permissions |
| `GET` | `/api/overview` | Dashboard metrics and chart series |
| `GET` | `/api/inventory` | Filtered, paginated current inventory |
| `GET` | `/api/inventory/export` | Full filtered, spreadsheet-safe current inventory export |
| `GET` | `/api/changes` | Filtered, paginated latest inventory changes |
| `GET` | `/api/changes/anomalies` | Current change-volume baselines and anomalies |
| `GET` | `/api/cost/anomalies` | Filtered daily cost anomalies, baseline evidence, and trend |
| `GET` | `/api/cost/anomalies/export` | Filtered, spreadsheet-safe anomaly export |
| `PUT` | `/api/cost/anomalies/review` | Admin-only anomaly investigation status |
| `GET` | `/api/reports/cost` | Native cost summary, breakdowns, and forecast |
| `GET` | `/api/reports/focus-cost` | Governed FOCUS charge-level investigation, coverage, and lineage |
| `GET` | `/api/reports/cost/export` | Filtered resource-level Cost Summary CSV |
| `GET` | `/api/reports/workload` | Native opportunity and retirement portfolio |
| `GET` | `/api/reports/workload/retirement/export` | Retirement candidate CSV |
| `GET` | `/api/reports/governance` | Azure Policy compliance posture |
| `GET` | `/api/reports/catalog` | Governed read-only measures and dimensions |
| `POST` | `/api/reports/catalog/validate` | Validate a report request against approved fields |
| `GET` | `/api/cost/anomalies/contributors` | Previous-week service/resource contributors |
| `GET` | `/api/evidence/opportunity` | Deterministic opportunity evidence package |
| `GET` | `/api/evidence/cost-anomaly` | Deterministic cost-anomaly evidence package |
| `GET` | `/api/recommendations/rightsizing` | Multi-source VM idle and Advisor-corroborated resize results |
| `GET` | `/api/recommendations/quality` | Advisor ID, semantic-action, resource-resolution, and actionability reconciliation |
| `GET` | `/api/integrations/finops-toolkit` | Imported Toolkit versions, checksums, and row counts |
| `GET` | `/api/opportunities` | Unified, filterable Azure Advisor and Flux Signals findings |
| `GET` | `/api/opportunities/export` | CSV export using the active opportunity filters |
| `GET` | `/api/integrations/azure` | Azure integration settings and status |
| `PUT` | `/api/integrations/azure` | Save Azure settings |
| `POST` | `/api/integrations/azure/sync` | Start inventory, Advisor, Flux Intelligence, and cost synchronization |
| `GET` | `/api/integrations/cost-reconciliation` | Compare current, historical, and commitment coverage per subscription |
| `GET` | `/api/integrations/cost-history` | Daily cost-history runs, retries, and scope state |
| `GET` | `/api/operations/health` | Admin-only source, worker, cost-completeness, and recommendation health |
| `POST` | `/api/intelligence/performance` | Attach browser round-trip and render timing to an intelligence request |
| `GET` | `/api/intelligence/review` | Admin-only recent prompt, reply, feedback, and stage-timing review |

Interactive API documentation is available at `/docs`.

## Data model

DuckDB is stored at `data/flux.duckdb` by default.

- `azure_integration` — one active Azure provider configuration.
- `sync_runs` — synchronization state and history.
- `sync_source_runs` — per-request, per-source and per-subscription attempts, row counts, retry attempts, and last-good retention status.
- `resource_snapshots` — append-only inventory and enrichment observations.
- `resources_current` — the newest complete inventory snapshot, so resources absent from a later ARG collection do not remain current.
- `cost_snapshots` / `costs_current` — actual and amortized month-to-date resource costs, retained independently for each successfully queried subscription and cost type.
- `daily_cost_history` — checkpointed actual and amortized daily cost grouped by resource and service, with collection lineage.
- `focus_import_runs`, `focus_export_manifests`, and `focus_cost_charges` — idempotent FOCUS v1.0 charge-level lineage, coverage, pricing, commitment, resource, and raw source evidence; current FOCUS dates take precedence over Query API rows.
- `cost_anomaly_runs`, `cost_anomaly_snapshots`, and `cost_anomalies_current` — method-versioned subscription, service, and resource evaluations; only anomalous findings and compact warming-up scopes are retained.
- `cost_anomaly_reviews` — administrator investigation state and notes, keyed to immutable anomaly evidence.
- `cost_history_runs` and `cost_history_scope_runs` — durable daily-history completion, failure, retry priority, and last-good status.
- `cost_details_backfill_scopes` — per-subscription, cost-type, and calendar-month checkpoints for the asynchronous Cost Details fallback.
- `policy_posture_snapshots` / `policy_posture_current` — assignment-level Azure Policy state summaries from ARG.
- `retail_price_snapshots` / `retail_prices_current` — append-only Azure Retail Prices attempts and last-good, unambiguous VM target rates keyed by region, SKU, OS/license profile, and currency.
- `commitment_cost_snapshots` / `commitment_costs_current` — actual month-to-date usage cost grouped by Meter ID and Pricing Model for the directional Reservation/Savings Plan eligible-cost view.
- `advisor_recommendation_snapshots` / `advisor_recommendations_current` — the latest complete active Advisor recommendation set.
- `rule_opportunity_snapshots` / `rule_opportunities_current` — versioned multi-finding Flux Signals observations with evidence and confidence.
- `opportunity_confidence_snapshots` / `opportunity_confidence_current` — reproducible heuristic scores built from persistence, corroboration, evidence freshness, and telemetry coverage.
- `opportunity_valuation_snapshots` / `opportunity_valuation_current` — method-versioned gross and risk-adjusted monthly values with current Cost Management run rate, target retail meter, Advisor fallback, and calculation lineage.
- `inventory_drift_runs`, `inventory_changes`, and `inventory_change_anomalies` — reproducible consecutive-snapshot diffs and scope-level robust statistical baselines.
- `rightsizing_recommendation_snapshots` / `rightsizing_recommendations_current` — per-telemetry-run VM coverage status, governed utilization evidence, candidate action, and savings lineage.
- `telemetry_metric_samples` — deduplicated incremental LogicMonitor CPU, memory, disk, and network observations with 30-day retention.
- `intelligence_usage_events` — 30-day request timing, token, cost, tool, error, feedback, and browser end-to-end telemetry.
- `intelligence_transcript_events` — 30-day administrator-reviewable prompts, validated replies, context, and raw final responses; model reasoning is excluded.
- `telemetry_collection_checkpoints` — per-LogicMonitor-device collection progress so later runs resume without re-reading long history.
- `source_sync_state` — successful collection markers that prevent a partial enrichment failure from replacing previously good source data.
- `finops_toolkit_*` — checksum-pinned Microsoft FinOps Toolkit v14 reference data and import provenance.

Resource snapshots include normalized fields for charting and filtering, raw ARG JSON, and nullable fields for:

- cost and source;
- utilization percentage and source;
- opportunity kind, reason, and estimated savings.

Cost, Advisor, and Flux Intelligence records retain their source, observation time, scope, and raw evidence. This makes LogicMonitor or another enrichment an adapter problem instead of a schema rewrite.

## Configuration

Copy `.env.example` values into your environment or deployment settings.

| Variable | Default | Purpose |
|---|---|---|
| `FLUX_HOST` | `127.0.0.1` | API bind host |
| `FLUX_PORT` | `8765` | API and built-frontend port |
| `FLUX_DUCKDB_PATH` | `data/flux.duckdb` | DuckDB file |
| `FLUX_FRONTEND_DIST` | `frontend/dist` | Built React assets |
| `FLUX_AZURE_PROVIDER` | `local_powershell` | Inventory provider |
| `FLUX_AZURE_POWERSHELL` | `pwsh` | PowerShell executable |
| `FLUX_AZURE_TIMEOUT_SECONDS` | `180` | ARG sync timeout |
| `FLUX_AZURE_MANAGEMENT_ENDPOINT` | `https://management.azure.com` | Azure Resource Manager endpoint and token scope |
| `FLUX_COST_MANAGEMENT_ENABLED` | `true` | Enable the independently scheduled actual/amortized cost collectors |
| `FLUX_COST_MANAGEMENT_API_VERSION` | `2025-03-01` | Cost Management Query and Generate Cost Details API version |
| `FLUX_COST_MANAGEMENT_TIMEOUT_SECONDS` | `120` | Timeout for each cost request |
| `FLUX_COST_MANAGEMENT_MAX_RETRIES` | `5` | Retry count for throttled or unavailable cost requests |
| `FLUX_COST_MANAGEMENT_REQUEST_DELAY_SECONDS` | `20` | Conservative base interval for shared, QPU-weighted pacing across cost jobs |
| `FLUX_COST_MANAGEMENT_CLIENT_TYPE` | `FluxFinOps` | Stable Cost Management client classification sent with every query |
| `FLUX_COST_MANAGEMENT_THROTTLE_COOLDOWN_SECONDS` | `30` | Additional pause after a persistent Cost Management 429 before the next scope |
| `FLUX_COST_HISTORY_INITIAL_DAYS` | `90` | One-time daily cost backfill window for a new subscription/cost type |
| `FLUX_COST_HISTORY_REFRESH_DAYS` | `14` | Rolling daily cost window refreshed after the first successful collection |
| `FLUX_COST_HISTORY_CHUNK_DAYS` | `14` | Maximum inclusive date span committed per daily-history transaction |
| `FLUX_COST_DETAILS_BACKFILL_ENABLED` | `true` | Use asynchronous Cost Details reports when a daily Query API scope fails |
| `FLUX_COST_DETAILS_MAX_REPORTS_PER_RUN` | `4` | Maximum monthly fallback reports generated by one daily job |
| `FLUX_COST_DETAILS_POLL_INTERVAL_SECONDS` | `20` | Default operation polling interval when Azure omits `Retry-After` |
| `FLUX_COST_DETAILS_MAX_POLL_ATTEMPTS` | `30` | Maximum polls for one asynchronous report |
| `FLUX_COST_DETAILS_CURRENT_REFRESH_DAYS` | `7` | Refresh cadence for a current-month fallback checkpoint |
| `FLUX_FOCUS_COST_ENABLED` | `true` | Enable independent FOCUS cost-export ingestion |
| `FLUX_FOCUS_STORAGE_ACCOUNT_URL` | `https://<account>.blob.core.windows.net` | Cost-export storage account |
| `FLUX_FOCUS_STORAGE_CONTAINER` | `cost-management` | Cost-export container |
| `FLUX_FOCUS_STORAGE_PREFIX` | `focus/` | Blob prefix scanned for manifests |
| `FLUX_FOCUS_LOCAL_PATH` | unset | Optional downloaded-export root for governed local backfill |
| `FLUX_FOCUS_MAX_MANIFESTS_PER_RUN` | `16` | Bound on new manifests imported by one worker run |
| `FLUX_COST_ANOMALY_LATENCY_DAYS` | `2` | Newest billed days excluded from anomaly evaluation |
| `FLUX_COST_ANOMALY_MINIMUM_HISTORY_DAYS` | `28` | Required age of a cost scope before classification |
| `FLUX_COST_ANOMALY_MINIMUM_BASELINE_POINTS` | `4` | Required matching-weekday observations |
| `FLUX_COST_ANOMALY_BASELINE_WEEKS` | `8` | Maximum prior matching weekdays in the seasonal baseline |
| `FLUX_COST_ANOMALY_THRESHOLD_K` | `3.5` | Robust median/MAD score required for an anomaly |
| `FLUX_COST_ANOMALY_MINIMUM_INCREASE` | `10` | Minimum daily absolute increase in the row currency |
| `FLUX_SYNC_WORKER_MODE` | `embedded` locally, `external` on App Service | Select the local embedded consumer or singleton continuous WebJob |
| `FLUX_SYNC_WORKER_POLL_SECONDS` | `5` | Queue polling interval for the durable sync worker |
| `FLUX_DRIFT_MIN_BASELINE_POINTS` | `5` | Completed drift intervals required before anomaly classification |
| `FLUX_DRIFT_MAD_THRESHOLD` | `3` | Median absolute deviation threshold for unusual change volume |
| `FLUX_RIGHTSIZING_MIN_WINDOW_DAYS` | `14` | Required governed telemetry evidence window |
| `FLUX_RIGHTSIZING_MIN_COVERAGE_PERCENT` | `70` | Minimum CPU sample coverage for classification |
| `FLUX_RIGHTSIZING_IDLE_CPU_P95` | `5` | Maximum CPU p95 for an idle candidate |
| `FLUX_RIGHTSIZING_IDLE_CPU_MAXIMUM` | `20` | Peak CPU guardrail that protects periodic workloads |
| `FLUX_RIGHTSIZING_IDLE_NETWORK_P95_BYTES` | `52428800` | Maximum hourly p95 for each network direction |
| `FLUX_RIGHTSIZING_REVIEW_CPU_P95` | `30` | Headroom threshold for Advisor-corroborated resizing |
| `FLUX_RIGHTSIZING_MEMORY_REVIEW_PERCENT` | `80` | Memory p95 guardrail that prevents an automatic action |
| `FLUX_RIGHTSIZING_CPU_DISAGREEMENT_PERCENT` | `20` | Maximum CPU p95 difference before independent sources require review |
| `FLUX_TELEMETRY_BOOTSTRAP_ROOT` | `./data/telemetry-bootstrap` locally; `/home/data/telemetry-bootstrap` in App Service | Root containing `logicmonitor` and `azure-monitor` historical extracts |
| `FLUX_LOGICMONITOR_ACCOUNT` | empty | LogicMonitor account subdomain |
| `FLUX_LOGICMONITOR_GROUP_IDS` | `4,5` | Linux and Windows device groups used for discovery |
| `FLUX_LOGICMONITOR_REQUEST_DELAY_MS` | `250` | Minimum spacing before LogicMonitor API requests |
| `FLUX_LOGICMONITOR_METRIC_BATCH_SIZE` | `12` | Least-recently-checkpointed matched devices per half-hour run |
| `FLUX_LOGICMONITOR_INITIAL_WINDOW_HOURS` | `8` | First incremental collection window for a matched device |
| `FLUX_LOGICMONITOR_MAXIMUM_WINDOW_HOURS` | `12` | Maximum catch-up window advanced by one run |
| `FLUX_LOGICMONITOR_METRIC_HISTORY_DAYS` | `14` | Rolling governed summary window |
| `FLUX_LOGICMONITOR_METRIC_RETENTION_DAYS` | `30` | Raw incremental sample retention |
| `FLUX_LOGICMONITOR_MAXIMUM_INSTANCES` | `8` | Per-datasource instance bound for disk and network collection |
| `LM_BEARER_TOKEN` | empty | LogicMonitor bearer token; production resolves this from Key Vault |
| `FLUX_INTELLIGENCE_SNAPSHOT_AGE_DAYS` | `30` | Age threshold for the aged-snapshot review rule |
| `FLUX_INTELLIGENCE_REQUIRED_TAGS` | empty | Comma-separated required allocation tags; empty retains the any-tag rule |
| `FLUX_INTELLIGENCE_TAG_EXCLUDED_TYPES` | empty | Comma-separated resource types excluded from tag findings |
| `FLUX_FINOPS_TOOLKIT_AHB_ENABLED` | `true` | Emit review-only Windows and SQL VM Hybrid Benefit eligibility findings adapted from Toolkit v14 |
| `FLUX_FINOPS_TOOLKIT_CACHE_ROOT` | `data/finops-toolkit` locally; `/home/data/finops-toolkit` in App Service | Verified Toolkit open-data download cache |
| `FLUX_RETAIL_PRICES_ENDPOINT` | `https://prices.azure.com/api/retail/prices` | Microsoft Azure Retail Prices endpoint |
| `FLUX_RETAIL_PRICES_API_VERSION` | `2023-01-01-preview` | Retail Prices API version |
| `FLUX_RETAIL_PRICES_TIMEOUT_SECONDS` | `30` | Timeout for one region/SKU/profile price request |
| `FLUX_RETAIL_PRICES_REQUEST_DELAY_MS` | `100` | Deliberate spacing between retail price requests |
| `FLUX_RETAIL_PRICES_REFRESH_HOURS` | `24` | Age before a previously attempted price key is refreshed |
| `FLUX_RETAIL_PRICES_HOURS_PER_MONTH` | `730` | Governed hourly-to-monthly target cost assumption |
| `FLUX_BACKUP_STORAGE_ACCOUNT_URL` | empty | Blob service URL; when set, successful syncs upload a checkpointed DuckDB backup |
| `FLUX_BACKUP_CONTAINER` | `flux-backups` | Private Blob container for database backups |
| `FLUX_BACKUP_RETENTION_DAYS` | `30` | Age after which Flux-owned backup blobs are pruned |
| `FLUX_MANAGED_IDENTITY_CLIENT_ID` | empty | Optional user-assigned managed identity client ID |
| `FLUX_AUTH_MODE` | `mock` | `mock`, `entra`, or `none` |
| `FLUX_ENTRA_TENANT_ID` | empty | Required tenant boundary in Entra mode |
| `FLUX_ENTRA_ADMIN_ASSIGNMENTS` | `Flux.Admin` | Admin app-role values or group IDs |
| `FLUX_ENTRA_READER_ASSIGNMENTS` | `Flux.Reader` | Reader app-role values or group IDs |
| `FLUX_INTELLIGENCE_AI_ENABLED` | `false` | Enables the assistant API |
| `FLUX_AI_PROVIDER` | `deepseek` | Provider adapter selection |
| `FLUX_DEEPSEEK_API_KEY` | empty | Backend-only provider credential; production uses a Key Vault reference |
| `FLUX_DEEPSEEK_CHAT_MODEL` | `deepseek-v4-flash` | Default assistant model |
| `FLUX_DEEPSEEK_BENCHMARK_MODEL` | `deepseek-v4-pro` | Explicit benchmark profile |
| `FLUX_AI_BUDGET_USD` | `10` | Evaluation budget |
| `FLUX_AI_STOP_AT_USD` | `8` | Estimated-cost stop/report threshold |
| `FLUX_AI_USAGE_RETENTION_DAYS` | `30` | Metadata-only usage retention |
| `FLUX_AI_TRANSCRIPT_RETENTION_DAYS` | `30` | Prompt/reply review retention; set to `0` to disable transcript storage |
| `FLUX_AI_SLOW_REQUEST_MS` | `20000` | End-to-end threshold used by Intelligence quality diagnostics |
| `FLUX_AI_MAX_TOOL_CALLS` | `12` | Maximum bounded governed tool calls per request |
| `FLUX_AI_TOOL_CACHE_SECONDS` | `30` | In-process TTL for identical bounded read-tool results |
| `FLUX_CORS_ORIGINS` | local Vite origins | Allowed development origins |
| `FLUX_DEV_SEED` | `false` | Seed demo data into an empty database |

## Verification

```powershell
python -m unittest discover -s .\tests -v
Set-Location .\frontend
npm run lint
npm run build
```

## Maturity

**Current maturity: focused internal alpha.**

| Area | Maturity | Notes |
|---|---|---|
| React application shell | Beta | Modular pages, responsive layout, production build, and route-level code splitting. |
| Dashboard | Beta | Rich inventory charts with explicit current/history/commitment scope reconciliation, a governed valued-action total, and honest telemetry coverage states. |
| ARG inventory | Beta | Paginated collection through local Az or App Service managed identity. |
| Azure Advisor | Beta | Active, untracked Cost and Performance recommendations are collected through ARG, semantically de-duplicated before storage, retain savings lineage, and are regression-checked by ID, semantic action, resource identity, and actionability. |
| Flux Signals | Alpha | Versioned deterministic ARG rules for VM state, disks, snapshots, Public IPs, NICs, NAT gateways, availability sets, NSGs, empty paid infrastructure, storage modernization, and tagging; findings retain evidence and confidence. |
| FinOps Toolkit compatibility | Alpha | MIT-attributed, checksum-pinned v14 reference datasets plus review-only Windows and SQL VM Hybrid Benefit rules with upstream lineage. |
| Opportunities | Beta | Default actionable queue, separate portfolio/evidence/governance views, semantic and source-ID de-duplication, Advisor/Flux corroboration, explicit right-sizing rationale, provenance-aware valuation, and filtered CSV export. |
| Inventory drift | Alpha | Exact create/delete/resize/retier/retag/move/reconfiguration diffs with method-versioned evidence and median/MAD scope baselines. |
| Synchronization operations | Beta | Independently scheduled source requests, persisted scope checkpoints, restart recovery, an administrator health center, next-run expectations, worker-stall detection, last-good retention, and one DuckDB-safe singleton worker. |
| Backup and recovery | Beta | Managed-identity Blob upload of checkpointed DuckDB files with prefix-scoped retention and a previously completed operator restore drill. |
| Cost Management | Beta | Resource cost plus Resource GUID/Pricing Model summaries, subscription/source checkpoints, persisted retry timing, Microsoft QPU retry-header support, partial-success retention, an automated Cost Details fallback, and an idempotent FOCUS v1.0 charge ledger for exported CSP subscriptions. |
| Cost anomalies | Beta | Durable per-scope history state, explicit billed-data latency, matching-weekday median/MAD evaluation, review workflow, CSV/evidence export, and explainable subscription/service/resource findings. Usage/rate decomposition remains planned. |
| Native reporting | Alpha | Currency-safe cost summary, governed forecast, workload/retirement portfolio, Azure Policy posture, and a curated reporting catalog. |
| Azure Monitor telemetry | Alpha | Independent six-hour WebJob collects rolling 14-day VM CPU, network, and disk summaries in bounded batches; coverage is reconciled by subscription and VM detail exposes the governed decision rationale. |
| Multi-source right-sizing | Alpha | Every VM receives an explicit coverage state. CPU, memory, and network evidence is reconciled conservatively; material source disagreement requires review, and resize targets require Azure Advisor corroboration. |
| LogicMonitor | Alpha | Key Vault-backed authentication, independent identity discovery, rotating per-device checkpoints, deduplicated CPU/memory/disk/network samples, governed retention, and rolling summaries with lineage. |
| DuckDB model | Beta | Append-only resource, cost, Advisor, rule-finding, telemetry-run, metric-summary, and source-match history with current views. |
| Entra authorization | Beta | Easy Auth principal decoding, tenant validation, app-role/group mapping, and API enforcement. |
| Managed identity | Beta | Secretless ARG, Advisor, and Cost Management calls with system- or user-assigned identity selection. |
| Azure administration | Beta | Admin-only subscription configuration and synchronization. |
| Price enrichment | Alpha | Explicit Advisor VM target SKUs are matched to unambiguous Microsoft retail hourly meters with OS/AHB handling, daily cache refresh, and separate actual-versus-retail lineage. Contracted rates remain planned. |
| Rill | Alpha/optional | Local read-only external-DuckDB connector, governed cost/anomaly models, metrics views, and explores; not a production dependency. |
| Ask Flux assistant | Alpha | Governed aggregate and FOCUS cost investigation, fast/deep-analysis routing, safe response repair/fallback, GFM Markdown/chart/Mermaid outputs, server-owned evidence links, deterministic per-answer quality scoring, bounded tool caching, administrator transcript review, and explicit stage bottleneck diagnostics. |

## Production gaps

Before production deployment:

1. Configure and validate Easy Auth, app-role assignments, managed identity, and subscription RBAC in the target App Service.
2. Ensure the application cannot be reached through a route that bypasses Easy Auth.
3. Connect structured operational health to an approved notification destination.
4. Monitor the incremental LogicMonitor collector through its first 14-day rolling-history warm-up and tune batch size against observed API limits.
5. Assign a non-human Flux.Reader identity before enabling the authenticated branch of the production smoke script in CI.
6. Complete model-service procurement/privacy review, adversarial evaluation,
   stakeholder acceptance criteria, distributed budget enforcement, and
   service-failover design before wider use of Flux Intelligence.
