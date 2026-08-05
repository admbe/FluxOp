# Optional Rill project

This project provides a read-only local Rill exploration layer over Flux's
DuckDB database. It is optional: the Flux application and native Reports page
remain the supported production reporting surface.

Run it from the repository root with:

```powershell
rill start .\rill
```

The connector attaches `data/flux.duckdb` read-only and materializes governed
cost and anomaly models into Rill's own embedded database. Stop the Flux
writers before first-time troubleshooting if the local operating system denies
concurrent access.

Do not publish this project to Rill Cloud as-is. External DuckDB attachment is
intended for local development, the application database may exceed Rill
Cloud's file limits, and Flux Entra authorization is not inherited by Rill.

Cost type and currency are explicit dimensions. Never combine currencies or
ActualCost and AmortizedCost into a single financial result.

The project includes governed cost, anomaly, workload-optimization, and Azure
Policy semantic models. `tests/test_report_contracts.py` compiles every model
against a freshly initialized Flux schema and compares the primary cost measure
with the native Cost Summary result. This validation is required before a Rill
measure changes.

The connector is read-only and exposes no App Service settings, Entra tokens,
Key Vault values, LogicMonitor credentials, or mutable integration tables.
