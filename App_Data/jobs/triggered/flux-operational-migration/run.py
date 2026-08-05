from __future__ import annotations

import json
import os
from pathlib import Path
import sys


application_root = Path("/home/site/wwwroot")
if not (application_root / "api").exists():
    application_root = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(application_root))

from api.operational_migration import migrate_operational_state


database_url = os.getenv("FLUX_OPERATIONAL_DATABASE_URL", "").strip()
if not database_url:
    raise SystemExit("FLUX_OPERATIONAL_DATABASE_URL is not configured.")

results = migrate_operational_state(
    duckdb_path="/home/data/flux.duckdb",
    database_url=database_url,
    replace=True,
)
if not results:
    raise SystemExit("No operational tables were found to migrate.")

print(
    json.dumps(
        {
            "status": "succeeded",
            "source": "/home/data/flux.duckdb",
            "tables": [
                {
                    "table": result.table,
                    "rowCount": result.row_count,
                    "checksum": result.checksum,
                }
                for result in results
            ],
        },
        separators=(",", ":"),
    ),
    flush=True,
)
