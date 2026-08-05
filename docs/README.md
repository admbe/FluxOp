# Flux documentation

One index for everything: architecture, data ingestion, FinOps methods, AI, and operations. Start with the [project README](../README.md) for product scope and quick start.

## Architecture and platform

| Document | Covers |
|---|---|
| [architecture.md](architecture.md) | System design, module boundaries, and the extension model |
| [POSTGRES-DUCKDB-INTERIM-SCALING-PLAN.md](POSTGRES-DUCKDB-INTERIM-SCALING-PLAN.md) | The interim scaling architecture: DuckDB as the analytical engine, a PostgreSQL operational store, and immutable analytics snapshots |
| [postgres-duckdb-migration-inventory.md](postgres-duckdb-migration-inventory.md) | Table-by-table state inventory backing the PostgreSQL transition |
| [entra-managed-identity.md](entra-managed-identity.md) | Microsoft Entra setup: app roles, managed identity, and RBAC deployment checklist |

## Cost data and ingestion

| Document | Covers |
|---|---|
| [FOCUS-COST-INGESTION.md](FOCUS-COST-INGESTION.md) | FOCUS v1.0 export ingestion: storage layout, manifests, and the importer contract |
| [AZURE-COST-MANAGEMENT-THROTTLING.md](AZURE-COST-MANAGEMENT-THROTTLING.md) | Query API throttling behavior and how collection stays inside it |
| [FINOPS-TOOLKIT-UPSTREAM.md](FINOPS-TOOLKIT-UPSTREAM.md) | Checksum-pinned Microsoft FinOps Toolkit reference data |

## FinOps methods

| Document | Covers |
|---|---|
| [FINANCIAL-PLANNING-FORECAST-METHOD.md](FINANCIAL-PLANNING-FORECAST-METHOD.md) | The governed forecasting method behind the fiscal-year outlook |
| [FLUX-SIGNAL-RULES.md](FLUX-SIGNAL-RULES.md) | The versioned read-only rules that produce Flux Signals findings |
| [FINOPS-RULE-TRADEOFF-SIMULATOR.md](FINOPS-RULE-TRADEOFF-SIMULATOR.md) | Trade-off simulation for rule thresholds |
| [REPORTING-PARITY.md](REPORTING-PARITY.md) | Parity mapping between native Flux reports and Microsoft FinOps Toolkit reports |

## AI

| Document | Covers |
|---|---|
| [FLUX-INTELLIGENCE.md](FLUX-INTELLIGENCE.md) | Ask Flux: governed tool surface, response validation, retention, and performance accounting |

## Operations

| Document | Covers |
|---|---|
| [VIRTUAL-TAGS.md](VIRTUAL-TAGS.md) | Virtual tag rules, imports, and provenance |
| [VIRTUAL-TAG-PRODUCTION-DEPLOYMENT.md](VIRTUAL-TAG-PRODUCTION-DEPLOYMENT.md) | Rolling virtual tags out to production |
| [FEATURE-CHECKLIST.md](FEATURE-CHECKLIST.md) | Feature inventory and verification checklist |
