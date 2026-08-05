"""One-time, non-destructive import of CSP usage-only daily exports.

The source files are not FOCUS exports.  They are promoted only to
``ActualCost`` and never replace an existing daily row (including FOCUS or
Cost Management rows).  Re-running the same files is therefore idempotent.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import duckdb
from filelock import FileLock


REQUIRED_COLUMNS = {
    "Azure Subscription ID",
    "Usage Date",
    "Service Name",
    "Billing PreTax",
    "Billing Currency",
    "Resource URI",
}


def _connection(path: Path, temp_directory: Path) -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(str(path))
    connection.execute("SET threads = 1")
    connection.execute("SET preserve_insertion_order = false")
    connection.execute("SET memory_limit = '1536MB'")
    temp_directory.mkdir(parents=True, exist_ok=True)
    connection.execute("SET temp_directory = ?", [str(temp_directory)])
    connection.execute("SET max_temp_directory_size = '8GB'")
    return connection


def _validate_columns(connection: duckdb.DuckDBPyConnection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info('csp_stage')").fetchall()
    }
    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise ValueError("CSP export is missing columns: " + ", ".join(sorted(missing)))


def import_files(database_path: Path, files: Iterable[Path], *, dry_run: bool = False) -> list[dict[str, object]]:
    database_path = database_path.resolve()
    temp_directory = database_path.parent / ".duckdb-tmp"
    results: list[dict[str, object]] = []
    with FileLock(str(database_path) + ".writer.lock", timeout=0):
        connection = _connection(database_path, temp_directory)
        try:
            for csv_path in files:
                csv_path = csv_path.resolve()
                if not csv_path.exists():
                    raise FileNotFoundError(csv_path)
                connection.execute(
                    """
                    CREATE OR REPLACE TEMP TABLE csp_stage AS
                    SELECT * FROM read_csv(?, header = true, all_varchar = true,
                                           auto_detect = true, ignore_errors = false)
                    """,
                    [str(csv_path)],
                )
                _validate_columns(connection)
                summary = connection.execute(
                    """
                    SELECT count(*), min(try_cast("Usage Date" AS TIMESTAMP)::DATE),
                           max(try_cast("Usage Date" AS TIMESTAMP)::DATE),
                           count(DISTINCT lower(trim("Azure Subscription ID"))),
                           coalesce(sum(try_cast(replace("Billing PreTax", ',', '') AS DOUBLE)), 0)
                    FROM csp_stage
                    WHERE try_cast("Usage Date" AS TIMESTAMP) IS NOT NULL
                    """
                ).fetchone()
                if not summary or not summary[0]:
                    raise ValueError(f"{csv_path.name} contains no valid usage rows")
                if not dry_run:
                    snapshot_id = f"csp-usage-{csv_path.stem.lower()}"
                    connection.execute(
                        """
                        CREATE OR REPLACE TEMP TABLE csp_aggregate AS
                        SELECT try_cast("Usage Date" AS TIMESTAMP)::DATE AS usage_date,
                               lower(trim("Azure Subscription ID")) AS subscription_id,
                               coalesce(nullif(lower(trim("Resource URI")), ''),
                                        '/subscriptions/' || lower(trim("Azure Subscription ID"))) AS resource_id,
                               coalesce(nullif(trim("Service Name"), ''), 'unallocated') AS service_name,
                               sum(try_cast(replace("Billing PreTax", ',', '') AS DOUBLE)) AS amount,
                               coalesce(nullif(trim("Billing Currency"), ''), 'USD') AS currency
                        FROM csp_stage
                        WHERE try_cast("Usage Date" AS TIMESTAMP) IS NOT NULL
                          AND nullif(trim("Azure Subscription ID"), '') IS NOT NULL
                        GROUP BY 1, 2, 3, 4, 6
                        """
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO daily_cost_history
                        SELECT ?, current_timestamp, usage_date, 'ActualCost',
                               subscription_id, resource_id, service_name, amount,
                               currency,
                               'csp_usage_export'
                        FROM csp_aggregate
                        """,
                        [snapshot_id],
                    )
                    inserted = connection.execute(
                        "SELECT count(*) FROM daily_cost_history WHERE snapshot_id = ?",
                        [snapshot_id],
                    ).fetchone()[0]
                else:
                    inserted = 0
                results.append(
                    {
                        "file": csv_path.name,
                        "rows": int(summary[0]),
                        "period_start": str(summary[1]),
                        "period_end": str(summary[2]),
                        "subscriptions": int(summary[3]),
                        "billing_pre_tax": float(summary[4]),
                        "inserted": int(inserted),
                    }
                )
            if not dry_run:
                connection.commit()
        finally:
            connection.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--csv", type=Path, action="append", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    results = import_files(args.database, args.csv, dry_run=args.dry_run)
    print(f"CSP usage import {'validation' if args.dry_run else 'completed'} at {datetime.now(timezone.utc).isoformat()}")
    for result in results:
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
