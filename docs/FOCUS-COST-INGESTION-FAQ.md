# FOCUS & Cost Ingestion FAQ

**Scope:** How every dollar reaches Flux — the 3 trucks (Query / Cost Details / FOCUS) plus local replay, throttling, and where `prod-eu-iaas-sub` sits.
**Canonical sources:** `docs/FOCUS-COST-INGESTION.md` (method + two-plane diagram), `docs/AZURE-COST-MANAGEMENT-THROTTLING.md` (quotas, ledger, alerts), `scripts/provision_focus_exports.ps1` + `provision_focus_export_identity.ps1` (identity), `api/cost.py` + `api/cost_details.py` + `api/jobs.py` + `api/focus.py`.
**Build:** `2.0.0` • commit in `GET /api/health` `commit` (`version.json` → `settings.build_commit` → header chip `v2.0.0 · shortCommit`).
**Last reviewed:** 2026-08-13 · **Status:** Implemented (Query + Cost Details gated on ledger; Export execution = Azure-scheduled daily; `flux-focus-cost` ingestion = every 6h). The FAQ below is the user-facing complement to the throttling spec's *Observability* section.

---

## 1. Where does cost start, before Flux touches it?

1. A VM / disk / SQL / Fabric capacity runs in a subscription (`prod-eu-iaas-sub` `6974eff9…`, etc.).
2. Azure meters it hourly (meterId, unit, region, sku).
3. **Azure Cost Management** aggregates that meter into two ledgers:
   - cost by day / resource / service (Query API's `ActualCost` + `AmortizedCost`);
   - normalized `BilledCost` / `EffectiveCost` / `ContractedCost` / `ListCost` per charge (FOCUS).
4. Microsoft refreshes those ledgers **~every 4 hours**. Pulling the same window more often re-reads the same data (`docs/AZURE-COST-MANAGEMENT-THROTTLING.md` § Cache rules).

Flux never creates cost — it only *reads*.

## 2. What starts the FOCUS export — and what does Flux own?

**Azure starts the FOCUS export. Flux only provisioned it once.**

- One-time provision: `scripts/provision_focus_exports.ps1` runs `az rest PUT https://management.azure.com/subscriptions/<id>/providers/Microsoft.CostManagement/exports/focus-daily?api-version=2025-03-01` per subscription with:
  `type FocusCost`, `timeframe MonthToDate`, `granularity Daily`, `format Csv`, `partitionData=true`, `dataOverwriteBehavior OverwritePreviousReport`.
- Schedule: `schedule.recurrence=Daily` `status=Active` `from 2026-08-02 → +10y` `location` from the storage account (`FOCUS-COST-INGESTION.md` § *Production source*).
- Delivery: `type AzureBlob` → `https://prodfinopswestus3sa.blob.core.windows.net` `container cost-management` `rootFolderPath focus/<label>` — **Cost Management's own first-party SP** writes the Csv + `manifest.json`, not a Flux identity.
- Identities: `scripts/provision_focus_export_identity.ps1` creates the non-interactive `Flux-FinOps-Export-Provisioner` (`Cost Management Contributor` per subscription + `Storage Blob Data Contributor` on `prodfinopswestus3sa`), used only by `azure-pipelines-focus-exports.yml` via WIF. Runtime ingestion uses the App Service **system-assigned managed identity** with `Storage Blob Data Reader` on that account to *read* the Blob.

After provisioning, Azure re-creates the export daily forever — Flux never re-triggers it.

## 3. Where does it export *from*?

One `focus-daily` export per **source subscription scope** `/subscriptions/<id>`:

- **16 active**: `dev-eu-iaas-sub`, `prod-eu-iaas-sub`, `prod-connectivity-sub`, `dev-uk-iaas-sub`, `prod-uk-iaas-sub`, `prod-uk-iaas-sub-ot`, `shared-services-sub`, `prod-example-sub`, `prod-uk-avd-sub`, `contoso-prod-sub`, `contoso-dev-sub`, `azure-subscription-1`, `apps-fabric-prod/adf/ado`, `contoso-sandbox-sub`, `apps-fabric-dev-sub`, `dev-team-sub` — map lives in `provision_focus_exports.ps1` `$subscriptions` (extended 2026-08-02 to 22-tenant estate).
- **2 excluded (Query-only)**: `contoso-test-sub` (`35dc1ae3…`) frozen 401 + deny assignment; `visual-studio-professional` (`d0ceb44b…`) WebDirect agreement rejects `FocusCost` outright ("not supported for Agreement Type: WebDirect" run 414).
- Re-running the provision script is idempotent — `SKIP` if export exists, `CREATE` if not.

## 4. The 4 ingestion paths (Query, Cost Details, FOCUS, local)

### Truck A — Cost Management Query API (`api/cost.py` — the daily workhorse)

- **Triggered by:** WebJob `flux-cost-history` (daily, `costHistoryScheduledFor: daily cost history collection`, per `flux-prod-wif`) + `flux-cost` (short freshness check). Schedule-driven, not Azure push.
- **What it sends:** `POST /subscriptions/<id>/providers/Microsoft.CostManagement/query?api-version=2025-03-01`
  `{ type: ActualCost|AmortizedCost, timeframe: Custom, timePeriod: {from,to}, dataset: {granularity: Daily, aggregation: {totalCost: Sum}, grouping: [ResourceId] } }`
  in **14-day chunks** (`cost_history_chunk_days=14`).
- **Throttling gate (from `AZURE-COST-MANAGEMENT-THROTTLING.md`):**
  - Checks `throttle_state.next_allowed_at` + rolling `cost_management_quota_state` (Flux operating ceilings **6/10s, 30/min, 300/hour** = 50% of Microsoft's published **12/60/600** tenant QPU).
  - Estimates `QPU ≈ months in range` (Microsoft notes date-range + factors change cost — header `x-ms-ratelimit-microsoft.costmanagement-qpu-consumed` is authoritative, reconciled after `200`).
  - Enforces `20s × QPU` minimum + tenant-wide **single active request** (`SharedRequestGate` `cost-management` slot) + header-driven backoff (`-retry-after`, `Retry-After` respected verbatim, never shortened).
  - On `429/503` persists the exact server retry duration + `next_allowed_at`, re-queues scope with fair rotation (head-of-queue never blocks 24h). On `401/403` fail-fast, no retry.
- **Where it lands:** operational `cost_history_runs / cost_history_scope_runs / cost_history_request_attempts` (checkpoints) → analytical `daily_cost_history` → snapshot `analytics_publications` → web snapshot (`FLUX_ANALYTICS_SNAPSHOT_MODE=snapshot` in prod, `direct` on `dev host 192.0.2.10`).

### Truck B — Cost Details API (`api/cost_details.py` — the recovery van)

- Used only when Query fails for a scope or a single subscription-month needs repair.
- Async: `POST .../generateCostDetailsReport` (max 1-month, 13-month lookback) → poll `GET .../getOperationResults` until `Succeeded` → download blob. Respects `x-ms-ratelimit-microsoft.consumption-retry-after` + `Retry-After`. Capped at **4 reports per run** (`cost_details_max_reports_per_run=4`, `cost_history_refresh_days=14` freshness guard).
- Shares the same tenant ledger and writes the same `daily_cost_history` — a newer Cost Details row for the same day atomically supersedes a Query row.

### Truck C — FOCUS Blob (`api/focus.py` + `api/jobs.py focus-cost` — governed history)

- **Every 6 hours** `flux-focus-cost` (singleton lease) lists `BlobServiceClient.list_blobs` on `cost-management/focus/<label>/*manifest.json`, downloads only new `runId` (`manifest.json` + `charge/*.csv`).
- Validates `type FocusCost` + `runId/startDate/endDate`, normalizes (`BilledCost→actual`, `EffectiveCost→amortized`, upper currency, canonical service), upserts in **one writer-lease tx** (`focus_export_manifests` + `focus_cost_charges` keeping full source JSON per charge) → refreshes `focus_cost_current` (winning manifest per `(subscription, billing period)` — **idempotency key = `manifest.runId + period`**) → re-derives `daily_cost_history`.
- **Precedence:** a period covered by FOCUS is authoritative — a later Query refresh for the same day **never overwrites** it (prevents double-count). `focus_import_runs` lives on the **operational plane** (Postgres when `FLUX_OPERATIONAL_DATABASE_ENABLED=true`), so analytical snapshot restores never lose the ingest outcome. **Auth:** App Service MI `Storage Blob Data Reader` on `prodfinopswestus3sa` only (alias C-60 `SPA catch-all returns 200 HTML for unknown /api/*` still open elsewhere).

### Truck D — Local replay (same transaction, no Blob)

```bash
export FLUX_FOCUS_LOCAL_PATH=/tmp/my-downloaded/focus
python -m api.jobs focus-cost --dry-run   # validate without writing
python -m api.jobs focus-cost             # real import
python -m api.jobs focus-cost --force     # re-import same period
```

Same validation + DB transaction as the Blob path; source is the local folder. `in/cost-management/` is gitignored.

## 5. Where all the trucks meet

```
Azure meters → Azure Cost Management (Query + Cost Details) + FOCUS Blob (daily Csv)
       ↓ 14d chunks with 429/503 backoff            ↓ every-6h copy
 operational: cost_history_* + throttle_state        operational: focus_import_runs
       ↘                                    ↙
          daily_cost_history (analytical DuckDB — resource/service/date aggregates)
                         ↓
              analytics snapshot → web snapshot (read-only)
                         ↓
 /api/reports/fiscal-outlook (FY) + overview MTD + cost-reconciliation + anomalies + optimizer + virtual tags + Rill + Ask Flux
 (Ask Flux tools get_focus_cost / investigate_cost_change read focus_cost_current + lineage; missing CSP contracted/list fields are returned unavailable, never inferred — FOCUS-COST-INGESTION.md)
```

## 6. Throttling, queues, and fairness (why `429` stops re-reading the same sub)

See the full policy in `AZURE-COST-MANAGEMENT-THROTTLING.md`. Summary here:

- One **tenant-wide permit ledger** keyed by tenant (`cost_management_quota_state` + `throttle_state` on the operational plane); Query, Cost Details, and (when Flux automates it) Export execution contend for the same budget.
- Rolling reservations (10s/60s/1h) + server `retry-after` → `next_allowed_at` are persisted header-driven, never shortened.
- Work priorities: current-period coverage repair → scheduled Export ingestion → freshness checks → historical backfill → Cost Details fallback → ad-hoc sync (ad-hoc queued behind background budget, not bypassing).
- Idempotency: don't re-query the same scope/type/window inside its freshness period; complete historical invoice periods cached; current period at most once per day unless explicit refresh.

## 7. How to check it (3 real checks — corrected)

> **Note:** docs earlier listed `GET /api/integrations/focus/status` — that route **does not exist** (`Unknown API path` is the SPA `200`-HTML catch-all per C-60). Use these three instead (patched in this FAQ).

| Question | Where to look | What you see | Reference |
|---|---|---|---|
| **Is Azure creating the file?** | `portal.azure.com` → Cost Management → Exports → `focus-daily` per subscription | `Type FocusCost`, `Frequency Daily`, `Destination cost-management / focus/<label>` `Active`, Last run status | `provision_focus_exports.ps1` § export creation |
| **Is Flux copying the file?** | `GET https://flux.example.com/api/operations/pipeline` (Entra auth) → `sourceFreshness` + WebJob `flux-focus-cost` `latest_run` in Kudu `https://fluxfinops-*.scm.azurewebsites.net/api/triggeredwebjobs/flux-focus-cost` | `focus_import_runs` outcome (`success`), `manifest_count`, `error` (429 vs 401/403 role hint), freshness age; staleness banner is `sourceSyncState.observed_at` | `FOCUS-COST-INGESTION.md` § Production source; `api/operational_store.py` |
| **What dollars came out?** | `GET /api/reports/focus-cost?startDate=…&endDate=…` (+ optional `&subscriptionId=b79d098f...`) and `GET /api/reports/focus-analytics` (both auth-guarded; unknown `/api/*` → `200` HTML is C-60) | Charge rows + manifest lineage (`runId`, `period`, currency `billing_currency`), reconciled `billed/effective` totals per `focus_export_manifests`; `GET /api/health` shows `analyticsReadMode` + `commit` | `api/main.py:1071,1240` |
| **Local replay** | `FLUX_FOCUS_LOCAL_PATH=/tmp/focus python -m api.jobs focus-cost --dry-run` | Same validation/transaction as Blob ingest | `FOCUS-COST-INGESTION.md` § Local verification |

See the throttling doc's **Observability and alerting** (requested for `20 - Projects/Flux`) for the 12 signals + 6 alerts:

> *Request count by endpoint/tenant/subscription/client type; estimated QPU vs `qpu-consumed`; `qpu-remaining` per window; 429 count + causing header; 503 + `Retry-After`; tenant cooldown + `next_allowed_at`; queue depth + oldest scope; coverage by subscription/month; time since last refresh; Cost Details / Export age.*
> Alert on `3 consecutive 429s`, `cooldown >15m`, `deferred >24h`, `coverage < target`, `actual >> estimated`, `same scope repeatedly fails while others healthy`.

## 8. `prod-eu-iaas-sub` without FOCUS — answered plainly

- **Without FOCUS, `prod-eu-iaas-sub` is still covered for daily MTD** — `cost-reconciliation` shows `280 rows, $4,518 MTD` succeeding on the daily 14-day Query path. Kill FOCUS and you lose `EffectiveCost` precision, not the subscription.
- **The FY exclusion is monthly history, not FOCUS** — `GET /api/reports/fiscal-outlook` `subscriptionCoverage 15/22`, `prod-eu-iaas-sub (6974eff9…)` `no_monthly_history` `coverageState:collection_failed` `lastIngestion 429 2026-07-25`. Monthly history uses a **12-month Query costing ~12 QPU** and hit the tenant `429` (throttle doc § Diagnosis: 5 direct 429 checks proved why ledger + `next_allowed_at` are needed); daily chunks at **1– месяца** succeeded later. `DailyAmortizedCost` still starts **117 days after** `DailyActualCost` (C-57). Fix is a targeted **monthly backfill retry / daily-rollup-as-monthly fallback** (audit `FLUX-B-011`/`C-53`), not a FOCUS toggle. `GET /api/reports/fiscal-outlook` `historyMonths >= 1` + `15/22 → 16/22` + band `$1.555M–$3.493M` narrowing is the signal the fix worked. Re-imaging `dev host 192.0.2.10` is explicitly out of scope per throttling doc § Operational notes.

## 9. Operational notes

- `version.json` stamped in `azure-pipelines.yml` + `frontend/dist/version.json` → `settings.build_commit` → `GET /api/health` `commit` + `GET /api/session` `build` → Shell header chip `v2.0.0 · commit`. Ports: dev Flux `8765` (`FLUX_PORT`), Rill `8786`, prod `0.0.0.0:8000` (pipeline safety-net `FLUX_HOST`/`FLUX_PORT`/`WEBSITE_SKIP_RUNNING_KUDUAGENT`/`FLUX_AUTH_MODE`).
- **`throttle_state` / `cost_history_*` / `cost_history_request_attempts`** = operational (Postgres `pg-flux-prod`); **`daily_cost_history`** = analytical snapshot — **web reads the last approved snapshot**.
- See `docs/FLUXOPS-GOAL.md` for 7-day window scoring (Expected/Actual/Success%/Freshness, `GREEN ≤1.5× interval`).

## 10. References

- `docs/FOCUS-COST-INGESTION.md`
- `docs/AZURE-COST-MANAGEMENT-THROTTLING.md`
- `docs/POSTGRES-DUCKDB-INTERIM-SCALING-PLAN.md`
- `scripts/provision_focus_exports.ps1` · `scripts/provision_focus_export_identity.ps1`
- `FLUX-INTELLIGENCE.md` · `COMMITMENT-OPTIMIZER.md` · `api/operational_store.py:418-455`
- Microsoft QPU / retry headers (`x-ms-ratelimit-microsoft.costmanagement-qpu-*`, `Retry-After`) as documented in the throttling doc § *Microsoft-published limits*.
- Known platform gaps surfaced while validating live `flux.example.com` in this chat: **C-60** `SPA catch-all returns 200 HTML for unknown /api/* (masks 404s)`, **C-52** `4 stale Kudu WebJobs suppress scheduled runs`, **C-53/K-06** `prod-eu-iaas-sub monthly 429`, **C-57** `DailyAmortized gap`, **C-56** `stale/degraded sources` — plus the `budget_group_coverage()` zero-caller audit note.
