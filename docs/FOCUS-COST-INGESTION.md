# FOCUS cost ingestion

## Purpose

Flux ingests Microsoft Cost Management FOCUS v1.0 exports as the governed
charge-level cost source. This complements the Cost Management Query API:

- FOCUS retains purchase and usage charges with resource, service, SKU, meter,
  pricing, commitment, tag, and source-lineage fields.
- `BilledCost` is promoted to daily `ActualCost`.
- `EffectiveCost` is promoted to daily `AmortizedCost`.
- Query API data continues to cover subscriptions without a successful export.
- A successful FOCUS period has precedence, so a later Query API refresh cannot
  overwrite or double-count that period.

## Production source

| Setting | Default |
|---|---|
| Account | `https://<your-storage-account>.blob.core.windows.net` |
| Container | `cost-management` |
| Prefix | `focus/` |
| Authentication | FluxFinOps system-assigned managed identity |
| Required role | Storage Blob Data Reader at the storage-account scope |
| Schedule | Every six hours |

The WebJob only lists manifests and downloads previously ungoverned runs.
Microsoft Cost Management remains responsible for the daily export schedule.

## Storage model

- `focus_import_runs` records each worker outcome.
- `focus_export_manifests` records export identity, period, lineage, coverage,
  row count, byte count, currency, and reconciled billed/effective totals.
- `focus_cost_charges` retains normalized analytical fields and the complete
  source row as JSON for future schema evolution.
- `focus_cost_current` exposes only the current imported manifest for each
  subscription and period.
- `daily_cost_history` receives resource/service/date aggregates used by native
  reports, forecasts, anomalies, evidence packs, Rill, and Flux Intelligence.

Manifest path and charge IDs make imports idempotent. A newer manifest for the
same subscription and period atomically supersedes the previous run.

## Local verification and backfill

Set `FLUX_FOCUS_LOCAL_PATH` to the root containing downloaded export folders,
then run:

```powershell
python -m api.jobs focus-cost
```

Local and Azure Blob imports use the same validation and database transaction.
Source files are never committed; `in/cost-management/` is ignored.

## Current limitations

- Only subscriptions with configured FOCUS exports have charge-level coverage.
- CSP `ListCost` is not treated as a savings baseline when the provider supplies
  zero or incomplete values.
- Contracted-price reporting remains unavailable where CSP exports omit usable
  contracted/list price evidence.
