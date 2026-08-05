from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import duckdb

from .operational_store import OperationalStore


MIGRATION_VERSION = "2026-07-27-operational-control-plane-v1"
OPERATIONAL_TABLES = (
    "azure_integration",
    "sync_runs",
    "sync_source_runs",
    "focus_import_runs",
    "intelligence_usage_events",
    "intelligence_transcript_events",
    "cost_anomaly_reviews",
    "cost_history_runs",
    "cost_history_scope_runs",
    "cost_history_request_attempts",
    "cost_details_backfill_scopes",
)


@dataclass(frozen=True)
class TableMigrationResult:
    table: str
    row_count: int
    checksum: str


def _table_exists(connection: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(
        connection.execute(
            """
            SELECT count(*)
            FROM information_schema.tables
            WHERE table_schema = 'main' AND table_name = ?
            """,
            [table],
        ).fetchone()[0]
    )


def _columns(connection: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    return [
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info('{table}')").fetchall()
    ]


def _checksum(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    payload = {
        "columns": columns,
        "rows": rows,
    }
    encoded = json.dumps(
        payload,
        default=str,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def migrate_operational_state(
    duckdb_path: Path,
    database_url: str,
    *,
    replace: bool = False,
    batch_size: int = 500,
) -> list[TableMigrationResult]:
    """Copy the legacy DuckDB operational state into PostgreSQL.

    Replacement is deliberately explicit because the operation truncates only
    the PostgreSQL-owned operational tables. The DuckDB source remains
    read-only and unchanged so it can serve as the rollback artifact.
    """
    if not replace:
        raise ValueError("Operational migration requires replace=True.")
    if not database_url.strip():
        raise ValueError("A PostgreSQL database URL is required.")
    source_path = Path(duckdb_path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    source = duckdb.connect(str(source_path), read_only=True)
    try:
        extracted: list[tuple[str, list[str], list[tuple[Any, ...]]]] = []
        for table in OPERATIONAL_TABLES:
            if not _table_exists(source, table):
                continue
            columns = _columns(source, table)
            rows = list(
                source.execute(
                    f'SELECT * FROM "{table}" ORDER BY ALL'
                ).fetchall()
            )
            extracted.append((table, columns, rows))
    finally:
        source.close()

    store = OperationalStore(
        database_url=database_url,
        duckdb_path=source_path.with_name("unused-operational.duckdb"),
    )
    store.init()
    results: list[TableMigrationResult] = []
    with store.connect() as target:
        if extracted:
            target.execute(
                "TRUNCATE TABLE "
                + ", ".join(f'"{table}"' for table, _, _ in extracted)
            )
        for table, columns, rows in extracted:
            quoted_columns = ", ".join(f'"{column}"' for column in columns)
            placeholders = ", ".join("?" for _ in columns)
            statement = (
                f'INSERT INTO "{table}" ({quoted_columns}) '
                f"VALUES ({placeholders})"
            )
            for offset in range(0, len(rows), max(1, batch_size)):
                target.executemany(
                    statement,
                    rows[offset : offset + max(1, batch_size)],
                )
            target_count = int(
                target.execute(
                    f'SELECT count(*) FROM "{table}"'
                ).fetchone()[0]
            )
            if target_count != len(rows):
                raise RuntimeError(
                    f"{table} validation failed: source={len(rows)}, "
                    f"target={target_count}."
                )
            results.append(
                TableMigrationResult(
                    table=table,
                    row_count=target_count,
                    checksum=_checksum(columns, rows),
                )
            )
        target.execute(
            """
            INSERT INTO schema_migrations (version, applied_at)
            VALUES (?, ?)
            ON CONFLICT (version) DO UPDATE SET
                applied_at = excluded.applied_at
            """,
            [MIGRATION_VERSION, datetime.now(timezone.utc)],
        )
    return results
