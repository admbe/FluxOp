from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.operational_migration import migrate_operational_state


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy legacy FluxFinOps operational state to PostgreSQL."
    )
    parser.add_argument(
        "--duckdb-path",
        default=os.getenv("FLUX_DUCKDB_PATH", "/home/data/flux.duckdb"),
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Required acknowledgement that PostgreSQL operational tables are replaced.",
    )
    args = parser.parse_args()
    database_url = os.getenv("FLUX_OPERATIONAL_DATABASE_URL", "").strip()
    results = migrate_operational_state(
        Path(args.duckdb_path),
        database_url,
        replace=args.replace,
    )
    print(
        json.dumps(
            {
                "status": "succeeded",
                "source": str(Path(args.duckdb_path)),
                "tables": [
                    {
                        "table": result.table,
                        "rowCount": result.row_count,
                        "checksum": result.checksum,
                    }
                    for result in results
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
