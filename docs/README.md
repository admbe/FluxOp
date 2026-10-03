# Flux documentation

> **Build `2.0.0` + `60d6a54`** — `version.json` is stamped at build time (pipeline step "Stamp build version"), resolved at runtime as `settings.build_commit` (`api/config.py: _resolve_build_commit`), and surfaced via `GET /api/health` (`commit`), `GET /api/session` (`build: {version, commit}` + `dataCurrency`), and the Shell header chip + footer (`frontend/src/components/Shell.tsx`). See `azure-pipelines.yml` and `docs/architecture.md` for the contract.

One index for everything: architecture, data ingestion, FinOps methods, AI, and operations. Start with the [project README](../README.md) for product scope and quick start, or [fluxop.ai](https://fluxop.ai) for the product overview.

**Ports (authoritative):** Flux dev **`8765`** — `FLUX_PORT`, `frontend/vite.config.ts` proxy, `playwright.config.ts` baseURL, `start-flux.ps1`; Rill dev **`8786`** (`rill/`); production `8000` (`FLUX_HOST=0.0.0.0`, `FLUX_PORT=8000`). **Databases:** local `data/flux.duckdb`; duckDB is single-writer (`data/flux.duckdb.writer.lock` plus a transient `*.wal`): a second process such as Rill opening the same file contends for the lock, so run `CHECKPOINT` before copying the file. App Service persistence is `/home/data/flux.duckdb`, not `wwwroot`; production jobs run as WebJobs.

## Architecture and platform

| Document | Covers |
|---|---|
| [architecture.md](architecture.md) | System design, module boundaries, ports (`8765`/`8786`/`8000`), version/build contract (`2.0.0`+`60d6a54`), and the extension model |
| [POSTGRES-DUCKDB-INTERIM-SCALING-PLAN.md](POSTGRES-DUCKDB-INTERIM-SCALING-PLAN.md) | The interim scaling architecture: DuckDB as the analytical engine, a PostgreSQL operational store, and immutable analytics snapshots |
| [postgres-duckdb-migration-inventory.md](postgres-duckdb-migration-inventory.md) | Table-by-table state inventory backing the PostgreSQL transition (with WAL/`writer.lock` guidance) |
| [entra-managed-identity.md](entra-managed-identity.md) | Microsoft Entra setup: app roles, managed identity, and RBAC deployment checklist (with `azure-pipelines.yml` pipeline-stamping context) |

## Cost data and ingestion

| Document | Covers |
|---|---|
| [FOCUS-COST-INGESTION.md](FOCUS-COST-INGESTION.md) | FOCUS v1.0 export ingestion: storage layout, manifests, and the importer contract |
| [FOCUS-COST-INGESTION-FAQ.md](FOCUS-COST-INGESTION-FAQ.md) | Four ingestion trucks (A Query API / B Cost Details / C FOCUS Blob / D local replay), FOCUS precedence, and `focus_import_runs` ledger |
| [COMMITMENT-OPTIMIZER.md](COMMITMENT-OPTIMIZER.md) | Hourly RI/SP portfolio simulation, manifest, gates, and scenario comparison |
| [AZURE-COST-MANAGEMENT-THROTTLING.md](AZURE-COST-MANAGEMENT-THROTTLING.md) | Query API throttling behavior and how collection stays inside it |
| [FINOPS-TOOLKIT-UPSTREAM.md](FINOPS-TOOLKIT-UPSTREAM.md) | Checksum-pinned Microsoft FinOps Toolkit reference data |

## FinOps methods

| Document | Covers |
|---|---|
| [FINANCIAL-PLANNING-FORECAST-METHOD.md](FINANCIAL-PLANNING-FORECAST-METHOD.md) | The governed forecasting method behind the fiscal-year outlook |
| [FLUX-SIGNAL-RULES.md](FLUX-SIGNAL-RULES.md) | The versioned read-only rules that produce Flux Signals findings |
| [FINOPS-RULE-TRADEOFF-SIMULATOR.md](FINOPS-RULE-TRADEOFF-SIMULATOR.md) | Trade-off simulation for rule thresholds |
| [REPORTING-PARITY.md](REPORTING-PARITY.md) | Parity mapping between native Flux reports and Microsoft FinOps Toolkit reports |

## Intelligence

| Document | Covers |
|---|---|
| [FLUX-INTELLIGENCE.md](FLUX-INTELLIGENCE.md) | **Ask Flux**: the 19-tool governed catalog, the mutation boundary, provider configuration (DeepSeek / OpenRouter / Azure AI Foundry), output controls, retention, performance accounting, and known limitations |
| [FLUX-ANALYST.md](FLUX-ANALYST.md) | **Flux Analyst (draft)**: proactive, scheduled playbooks that file evidence-linked work products into a human review queue — 21 features across Inform/Optimize/Operate, route-allowlisted `flux-analyst-mi`, phased rollout (P1 needs no new Azure surface) |

## Operations

| Document | Covers |
|---|---|
| [VIRTUAL-TAGS.md](VIRTUAL-TAGS.md) | Virtual tag rules, imports, and provenance |
| [VIRTUAL-TAG-PRODUCTION-DEPLOYMENT.md](VIRTUAL-TAG-PRODUCTION-DEPLOYMENT.md) | Rolling virtual tags out to production |
| `50 - Procedures/Flux - Public GitHub Sanitization.md` (vault) | Allowlist sanitization + `GITHUB_TOKEN` in Infisical for `admbe/FluxOp` — synced 2026-08-25 `60d6a54`→`541c510` |
| [FEATURE-CHECKLIST.md](FEATURE-CHECKLIST.md) | Feature inventory and verification checklist |
| [FEATURE-IDEAS.md](FEATURE-IDEAS.md) | Lightweight inbox for new feature requests/ideas (Inbox → Accepted → Shipped) |
