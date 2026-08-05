# FluxFinOps PostgreSQL + DuckDB Migration Inventory

**Status:** discovery draft  
**Prepared:** 2026-07-26  
**Purpose:** map current repository data objects and access paths before any runtime cutover

This document is the first discovery artifact for the interim PostgreSQL + DuckDB migration plan. It captures what exists in the repository today, where the mutable state currently lives, and which code paths are the likely migration boundaries.

It is intentionally conservative:

- it does not change runtime behavior;
- it does not assume PostgreSQL schema names beyond the plan;
- it treats current DuckDB tables as the source of truth for present ownership;
- it marks target ownership as provisional where the codebase has not yet been refactored.

## 1. Repository state at discovery time

Current evidence in the repo:

- FluxFinOps uses DuckDB as the active application store today.
- There is no PostgreSQL driver or runtime PostgreSQL store module in `requirements.txt` or `api/`.
- DuckDB connection handling is centralized in `api/database.py`.
- The current schema is created and evolved inside `api/database.py`.
- Worker coordination, retry gating, analytics writes, and report reads still share the same DuckDB-backed runtime today.

## 1.1 Sprint progress since discovery

The first migration seam is now in place:

- `api/operational_store.py` provides a separate operational store abstraction.
- `api/database.py` now has an `operational_connect()` path and a dedicated operational backend name.
- Cost-history run state has been redirected away from the primary analytics file.
- The default local fallback for operational state is a separate DuckDB file, with PostgreSQL support wired through configuration for the interim architecture.

Still pending:

- broader synchronization and telemetry state cutover;
- PostgreSQL deployment configuration and live validation;
- removal of the remaining legacy operational tables from the main analytics file after the new path is proven.

## 1.2 Snapshot publication and consumption (plan sections 12-13)

Implemented 2026-07-28 in `api/analytics_snapshot.py`:

- `SnapshotPublisher` checkpoints the mutable database under the writer
  lease, validates an independent read-only candidate (critical tables,
  current-view smoke queries, row counts), checksums it, uploads it to
  snapshot storage, and records an approved row in `analytics_publications`
  (operational store). Rejected candidates are recorded and never approved;
  the previous version stays current. Retention pruning is applied on both
  the ledger and storage.
- `AnalyticsSnapshotManager` (web) adopts the newest approved publication:
  download, checksum verification, read-only smoke validation, then an atomic
  swap of `FluxDatabase.attach_read_snapshot()`. In-flight requests keep
  their open handles to the previous file.
- `FluxDatabase.connect(read_only=True)` routes to the attached immutable
  snapshot with **no cross-process lease and no in-process serialization**;
  writes keep the historical lease.
- Storage backends: private Blob (`FLUX_SNAPSHOT_STORAGE_ACCOUNT_URL`) or a
  local directory for development.
- Worker publishes after each completed synchronization and after each
  data-writing scheduled job (`_with_publication`); manual
  `python -m api.jobs publish-analytics-snapshot` also exists.
- Rollout is config-gated: `FLUX_ANALYTICS_SNAPSHOT_MODE=snapshot` plus
  `FLUX_ANALYTICS_SNAPSHOT_PUBLISH=true`; the default (`direct`) preserves
  current behavior. Web falls back to direct reads until the first approved
  publication exists.

Phase 6-7 production cutover completed 2026-07-29: publication version 1
(1.17 GB) approved in Blob, and the web serves analytical reads from the
adopted snapshot (`analyticsReadMode: snapshot` in `/api/health`).
Remaining: removing the direct-read web fallback once snapshot mode has
soaked.

## 1.3 PostgreSQL coordination and staged applies (plan Phases 4-5)

Implemented 2026-07-29:

- `OperationalStore.singleton_lease(name)`: session-scoped advisory locks
  replace all eight per-job file locks (sync worker, cost-history, focus,
  telemetry collectors, finops-toolkit, retail-prices); a crashed holder
  releases automatically. DuckDB development backend falls back to the
  historical file lock.
- Sync claims carry expiring ownership (`claimed_by`, `claim_expires_at`,
  `FLUX_SYNC_CLAIM_LEASE_SECONDS`) heartbeat-extended by stage updates,
  claimed with `FOR UPDATE SKIP LOCKED` on PostgreSQL; `finish_sync`
  validates ownership. Live claims cannot be stolen; expired claims
  recover.
- The Cost Management request gate moved from a local file to the shared
  `throttle_state` table (atomic slot claims, monotonic 429 cooldowns).
- Phase 5 framework in `api/analytics_writer.py`: durable
  `analytics_apply_jobs` with idempotency keys and payload checksums,
  gzip-staged payloads, a registry of per-source appliers, and
  `apply_pending` under the singleton analytics-writer lease. Duplicate
  delivery is a no-op; a checksum conflict fails closed; failures retry to
  a bounded terminal state; payloads survive crashes between staging and
  apply. The retail-prices collector is the migrated reference; remaining
  collectors (inventory/advisor snapshot, cost history, telemetry, FOCUS,
  toolkit open data) still write directly and migrate incrementally
  through the same registry.
- The deployment quiesce marker deliberately stays file-based: it is a
  pipeline-written deploy-time signal, not database coordination.

## 2. Current DuckDB object inventory

The schema currently defined in `api/database.py` includes the following tables.

### 2.1 Operational / mutable state

These are the strongest candidates for PostgreSQL ownership in the interim architecture:

- `azure_integration`
- `sync_runs`
- `sync_source_runs`
- `source_sync_state`
- `cost_history_runs`
- `cost_history_scope_runs`
- `cost_history_request_attempts`
- `cost_details_backfill_scopes`
- `focus_import_runs`
- `intelligence_usage_events`
- `intelligence_transcript_events`
- `cost_anomaly_reviews`

These tables represent configuration, claims, attempts, throttling, or user/admin mutation state. They are the main reason the current single DuckDB file is acting as both application database and analytical store.

### 2.2 Analytical / append-only state

These are the strong candidates to remain in DuckDB for the interim:

- `resource_snapshots`
- `cost_snapshots`
- `commitment_cost_snapshots`
- `daily_cost_history`
- `focus_export_manifests`
- `focus_cost_charges`
- `cost_anomaly_runs`
- `cost_anomaly_snapshots`
- `policy_posture_snapshots`
- `policy_resource_snapshots`
- `retail_price_snapshots`
- `advisor_recommendation_snapshots`
- `rule_opportunity_snapshots`
- `finops_toolkit_dataset_versions`
- `finops_toolkit_resource_types`
- `finops_toolkit_regions`
- `finops_toolkit_services`
- `finops_toolkit_pricing_units`
- `finops_toolkit_commitment_eligibility`
- `telemetry_runs`
- `telemetry_metric_summaries`
- `telemetry_metric_samples`
- `telemetry_collection_checkpoints`
- `resource_source_matches`
- `telemetry_resource_attempts`
- `opportunity_confidence_snapshots`
- `opportunity_valuation_snapshots_v2`
- `inventory_drift_runs`
- `inventory_changes`
- `inventory_change_anomalies`
- `rightsizing_recommendation_snapshots`

These tables are analytical history, derived evidence, or governed reporting inputs. They are consistent with the plan’s instruction to keep DuckDB as the analytical engine.

### 2.3 Schema ledger / migration bookkeeping

- `schema_migrations`

This table is a special case. It is not product state. It exists to track schema evolution in the current DuckDB-centric implementation. During the PostgreSQL transition, this likely becomes part of the migration toolchain rather than a business-owned runtime table.

## 3. Initial ownership map

This is the first-pass target ownership map for the interim architecture.

### 3.1 PostgreSQL target ownership

Planned PostgreSQL ownership includes:

- integration configuration;
- sync request and run coordination;
- child source claims and retries;
- throttle and attempt state;
- Cost Details backfill state;
- FOCUS import execution metadata;
- transcript and usage retention metadata;
- review / moderation state;
- any future publication metadata required to coordinate DuckDB snapshots.

### 3.2 DuckDB target ownership

Planned DuckDB ownership includes:

- append-only inventory history;
- append-only cost history;
- commitment and FOCUS analytical charges;
- recommendation and anomaly evidence;
- telemetry summaries and samples;
- inventory drift and rightsizing results;
- governed report views and analytical joins;
- toolkit reference data used in report semantics.

### 3.3 Split ownership

Some concepts will remain split:

- PostgreSQL: execution state, claim/retry state, publication metadata.
- DuckDB: derived analytical rows and governed report projections.
- Blob storage: immutable snapshot files and staged export artifacts.

## 4. Access-path inventory

The main repository access paths discovered so far are:

| Module | Current responsibility | Migration relevance |
|---|---|---|
| `api/database.py` | DuckDB schema, connection management, persistence helpers | Primary schema source and the first refactor boundary |
| `api/jobs.py` | Worker execution, source collection, analytics writes | Will split between PostgreSQL coordination and DuckDB apply work |
| `api/cost.py` | Cost request gating, throttling, retry timing | Candidate for PostgreSQL-backed durable throttle state |
| `api/synchronization.py` | Sync run orchestration and claim handling | Candidate for PostgreSQL-backed work claims |
| `api/focus.py` | FOCUS import lifecycle | Split between execution state in PostgreSQL and charges in DuckDB |
| `api/intelligence.py` | Flux Intelligence storage, response shaping, usage metadata | Candidate for PostgreSQL transcript/usage metadata plus DuckDB analytical views |
| `api/intelligence_assistant.py` | Assistant response contract and validation | Likely unchanged for the first migration pass |
| `api/telemetry.py` / `api/telemetry_import.py` | Telemetry collection and staging | Will remain analytical, but run metadata may move |
| `api/backup.py` / `api/recovery.py` | Backup and restore behavior | Will need post-cutover snapshot and rollback awareness |
| `api/report_catalog.py` | Governed reporting catalog | Should remain read-mostly and may stay DuckDB-backed initially |

## 5. Immediate migration implications

The discovery pass points to three high-value boundaries:

1. **Operational coordination boundary**  
   Move claims, retries, and throttle state out of DuckDB first so API and workers stop competing on the same mutable file.

2. **Analytics apply boundary**  
   Keep analytic rows in DuckDB, but make the writer the only process that mutates the analytics file.

3. **Snapshot boundary**  
   Add immutable snapshot publication and local read-only snapshot loading after the operational split is in place.

## 6. Suggested next implementation step

The next safe code step is to introduce storage abstractions so current services can be redirected without changing API behavior:

- `OperationalStore` for PostgreSQL-backed coordination state;
- `AnalyticsStore` for DuckDB-backed analytical writes and reads;
- `AnalyticsSnapshotManager` for publish/download/swap behavior.

That gives the codebase a seam for migration without forcing a full cutover in one change.

## 7. Open discovery items

These remain to be mapped in detail:

- exact call sites for every table in `api/database.py`;
- all worker claim/retry transitions in `api/jobs.py` and `api/synchronization.py`;
- the precise FOCUS import write path and any manifest dependencies;
- the transcript and usage retention lifecycle for Flux Intelligence;
- whether any current review or admin state is persisted outside `api/database.py`;
- the exact rollout order for PostgreSQL migrations and feature flags.

## 8. Validation baseline

Before changing runtime behavior, the following baseline should remain green:

- backend unit tests;
- frontend lint and browser smoke tests;
- authenticated Reader/Admin flow coverage;
- current DuckDB hardening tests.

Those tests establish that the migration starts from a known good state.

## 9. Post-cutover performance remediation

After the operational control plane moved to PostgreSQL, the dashboard became
noticeably slow to load. Root-cause analysis identified the read path, not the
migration itself, as the bottleneck.

### Finding

`OperationalStore.connect()` opened a **brand-new `pg8000` connection on every
call** — full TLS handshake plus authentication round-trip, 30s timeout, no
pooling, no caching. The hot read paths fan out into several helpers that each
opened their own connection:

| Request | Operational connections opened (sequential) |
|---|---|
| `GET /api/overview` | `latest_sync` (2) + `source_freshness` (1) + `cost_reconciliation` → `integration` (1) + attempt rows (1) ≈ **5** |
| `GET /api/operations/health` | `source_freshness` (1) + `cost_reconciliation` (2) + `latest_sync` (2) + queue query (1) ≈ **6** |

Each connection paid the full TLS+auth cost serially, and the admin overview
poll (`frontend/src/App.tsx`) re-fires `operationalHealth` every 2.5s during a
sync, multiplying the effect.

### Remediation

1. **Connection pool.** `OperationalStore` now maintains a bounded pool (default
   8, configurable via `FLUX_OPERATIONAL_POOL_SIZE`) backed by a LIFO queue and
   a capacity semaphore. Connections are validated on checkout with a `SELECT 1`
   ping and discarded/recreated when stale; read-only transactions are rolled
   back before release so the next caller receives a clean connection. Idle
   connections are reused instead of re-handshaking. (`api/operational_store.py`)

2. **Batched operational reads.** `latest_sync()` now fetches the latest sync run
   and its source runs in a single query (LEFT JOIN) instead of two sequential
   connections. `overview()`, `operational_health()`, `cost_reconciliation()`,
   `source_freshness()`, and `integration()` accept an optional shared
   `_operational_db` connection; the two dashboard endpoints open **one**
   operational connection and thread it through every helper instead of each
   helper opening its own. (`api/database.py`)

### Effect

- Per-request TLS+auth round-trips drop from ~5–6 to **1** on the two hot
  endpoints; remaining reads reuse the pooled connection.
- The 2.5s admin poll no longer re-handshakes on every tick.
- Standalone callers (jobs, sync, individual endpoints) are unchanged in shape
  and benefit from the pool transparently.

### Validation

- `python -m unittest discover -s tests -v` — 135 passed, 5 skipped (PostgreSQL
  integration tests require `FLUX_TEST_POSTGRES_URL`).
- `frontend && npm run lint` — clean.

### Remaining

- Throttle the `operationalHealth` poll to a slower cadence than the cheap
  `overview` poll (still tracked, not in this change).
- Add connection-pool metrics to the operational health center if needed.

## 10. DuckDB analytical query materialization

### Finding

A Chrome DevTools performance trace of the Reports page (captured before the
connection-pool fix was live) showed three endpoints taking ~96 seconds each,
with TTFB ≈ total — 100% server processing in DuckDB, 0% network transfer:

| Endpoint | TTFB | Total |
|---|---|---|
| `/api/reports/cost` | 96.6s | 96.9s |
| `/api/reports/workload` | 96.3s | 96.3s |
| `/api/reports/governance` | 95.4s | 95.5s |

The connection-pool fix (§9) does not affect these endpoints — they read only
DuckDB analytical tables, not the PostgreSQL control plane.

### Root cause

The four hottest "current" projections — `resources_current`,
`costs_current`, `commitment_costs_current`, and `policy_posture_current` —
were **non-materialized VIEWs**. Each recomputed `arg_max(snapshot_id,
observed_at)` window functions over `source_sync_state` + snapshot tables on
every reference. `cost_report` alone references `resources_current` 8+ times
per request; `overview` references it 10+ times. Additionally,
`daily_cost_history` had no sargable index for the `cost_type` + date-range
filter pattern used by `cost_report`.

### Remediation

1. **Materialized tables.** The four hot views are now `CREATE TABLE` (not
   `VIEW`). All existing `SELECT ... FROM resources_current` queries hit a
   pre-computed table instead of recomputing the window function each time.
   The remaining 10 `*_current` views stay as views (cold/compute-side paths).
   (`api/database.py`)

2. **Automatic refresh.** `store_snapshot` calls `_refresh_after_snapshot()`
   after commit, rebuilding only the affected tables (inventory →
   `resources_current`, costs → `costs_current`, commitment →
   `commitment_costs_current`). `store_policy_posture` rebuilds
   `policy_posture_current` after commit. `init()` calls
   `_refresh_materialized_tables_internal()` to populate on startup.
   (`api/database.py`)

3. **Migration-safe.** Existing databases with the old VIEWs are migrated
   automatically: `init()` checks `information_schema.tables.table_type` and
   drops any VIEW before creating the TABLE. Verified with a two-phase test
   (create old DB → re-init → tables created correctly).

4. **Sargable indexes.** Added:
   - `idx_daily_cost_history_type_sub_date` on
     `daily_cost_history(cost_type, subscription_id, usage_date)` — the
     `cost_report` filter pattern.
   - `idx_cost_snapshots_type_sub` on
     `cost_snapshots(cost_type, subscription_id)`.
   - `idx_resource_snapshots_snapshot` on `resource_snapshots(snapshot_id)`.
   - `idx_policy_posture_snapshots_snapshot` on
     `policy_posture_snapshots(snapshot_id)`.

### Expected effect

- The 96-second Reports-page queries should drop to single-digit seconds:
  each `resources_current` reference is now a table scan (or index lookup)
  instead of a window-function recomputation over the full snapshot history.
- `cost_report`'s `daily_cost_history` scans use the index instead of a full
  table scan.
- `overview` benefits from the same materialization (10+ `resources_current`
  references per request).

### Validation

- `python -m unittest discover -s tests -v` — 135 passed, 5 skipped.
- Migration test: old DB with VIEWs → re-init → tables created correctly.
- Round-trip test: `store_snapshot` → materialized tables immediately
  populated and readable.
- `frontend && npm run lint && npm run build` — clean.

### Note on deployment

The CI/CD pipeline Build stage passes with these changes. The Deploy stage
had a pre-existing Kudu ZIP Deploy failure (runs 292–307, all commits since
7/27) caused by the DuckDB file being inside `/home/site/wwwroot/data/` —
ZIP Deploy rsync cannot overwrite files locked by the running Python process.
Fixed by migrating DuckDB to `/home/data/flux.duckdb` (persistent storage
outside wwwroot) and enabling `WEBSITE_RUN_FROM_PACKAGE=1` so Kudu mounts
the ZIP read-only without extraction/rsync (commit `80c84ac`).

### Recurring DuckDB corruption and exact-version pin

Five DuckDB corruption incidents occurred in five days (2026-07-26 through
2026-07-31), each with a different internal error signature (`Failed to load
metadata pointer`, `invalid node type for TransformToDeprecated: 53`,
`Failed to create checkpoint ... No such file or directory`). Root cause: the
floating DuckDB version range (`>=1.3,<1.5`) let consecutive pipeline deploys
independently re-resolve to whatever patch build satisfied it at that build's
`pip install` time, so different engine builds wrote and checkpointed the same
on-disk file. Fixed by exact-pinning to `duckdb==1.4.5` in `requirements.txt`
(commit `a5bd787`).

### Lifespan refresh and operational-flag bypass

Commit `1204bef` added `database.refresh_current_views()` to the FastAPI
lifespan, called after `init()` regardless of `FLUX_OPERATIONAL_DATABASE_ENABLED`.
When that flag is `true` (production), `init()` is skipped entirely — so the
materialized `*_current` tables were never created, and the overview fell back
to expensive non-materialized VIEWs over the full snapshot history (~104s per
request). The lifespan-level refresh is best-effort (wrapped in try/except) so
the app starts even if DuckDB is corrupted or locked.

### DuckDB connection deadlock fix

Commit `b6d5091` fixed a deadlock where `overview()` opened a read-only DuckDB
connection (acquiring the cross-process writer lock) and then called
`source_freshness()` which opened a second read-only connection, deadlocking
on the same lock. The fix ensures `source_freshness()`, `cost_reconciliation()`,
and `latest_sync()` accept an optional shared `_operational_db` connection,
and the overview threads one operational connection through all sub-calls
instead of each opening its own.
