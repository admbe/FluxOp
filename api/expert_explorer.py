"""Expert Explorer: natural language to governed, validated DuckDB SQL.

The generation model proposes SQL; nothing it proposes is trusted. Every
statement passes a strict validator before execution:

- Single statement, SELECT/WITH only.
- Write, DDL, transaction, extension, and settings keywords are rejected
  outright, as are file/system table functions (read_csv, read_parquet,
  glob, ...), so a read-only connection cannot be used to reach the
  filesystem.
- Every referenced relation must be a semantic-layer view (or a CTE the
  query itself defines). The raw warehouse tables are not reachable.
- Results are capped by wrapping the query in an outer LIMIT, and
  execution runs under a watchdog that interrupts the connection when the
  timeout passes.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b("
    r"insert|update|delete|merge|truncate|create|drop|alter|replace|"
    r"attach|detach|copy|export|import|install|load|call|pragma|set|"
    r"reset|begin|commit|rollback|vacuum|checkpoint|grant|revoke|use"
    r")\b",
    re.IGNORECASE,
)
_FORBIDDEN_FUNCTIONS = re.compile(
    r"\b("
    r"read_csv|read_csv_auto|read_parquet|parquet_scan|read_json|"
    r"read_json_auto|read_text|read_blob|glob|getenv|sniff_csv|"
    r"parquet_metadata|duckdb_extensions|duckdb_settings|"
    r"read_ndjson|read_ndjson_auto|st_read|sqlite_scan|postgres_scan|"
    r"iceberg_scan|delta_scan|httpfs"
    r")\s*\(",
    re.IGNORECASE,
)
_CTE_NAMES = re.compile(r"(?i)(?:\bwith\b|,)\s*([a-zA-Z_][\w]*)\s+as\s*\(")
_RELATIONS = re.compile(r"(?i)\b(?:from|join)\s+([a-zA-Z_][\w.\"]*)")


class ExpertQueryError(ValueError):
    """A generated query failed validation or safe execution."""


def _strip_literals_and_comments(sql: str) -> str:
    """Remove string literals and comments so keyword scanning cannot be
    defeated by quoting, and quoted identifiers do not confuse parsing."""
    out: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        ch = sql[index]
        if ch == "'":
            index += 1
            while index < length:
                if sql[index] == "'" and index + 1 < length and sql[index + 1] == "'":
                    index += 2
                    continue
                if sql[index] == "'":
                    break
                index += 1
            index += 1
            out.append("''")
            continue
        if ch == "-" and sql[index : index + 2] == "--":
            while index < length and sql[index] != "\n":
                index += 1
            continue
        if ch == "/" and sql[index : index + 2] == "/*":
            end = sql.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        out.append(ch)
        index += 1
    return "".join(out)


def validate_expert_sql(sql: str, allowed_relations: set[str]) -> str:
    """Validate a generated statement; returns the trimmed SQL or raises."""
    candidate = (sql or "").strip().rstrip(";").strip()
    if not candidate:
        raise ExpertQueryError("The generated query was empty.")
    cleaned = _strip_literals_and_comments(candidate)
    if ";" in cleaned:
        raise ExpertQueryError("Only a single statement is allowed.")
    head = cleaned.lstrip().split(None, 1)
    if not head or head[0].lower() not in ("select", "with"):
        raise ExpertQueryError("Only SELECT queries are allowed.")
    keyword = _FORBIDDEN_KEYWORDS.search(cleaned)
    if keyword:
        raise ExpertQueryError(
            f"Forbidden keyword {keyword.group(1)!r} in the generated query."
        )
    function = _FORBIDDEN_FUNCTIONS.search(cleaned)
    if function:
        raise ExpertQueryError(
            f"Forbidden function {function.group(1)!r} in the generated query."
        )
    ctes = {name.lower() for name in _CTE_NAMES.findall(cleaned)}
    allowed = {name.lower() for name in allowed_relations}
    for relation in _RELATIONS.findall(cleaned):
        name = relation.strip().strip('"').lower()
        if "." in name:
            raise ExpertQueryError(
                f"Qualified relation {relation!r} is not allowed; only "
                "governed semantic views are queryable."
            )
        if name in ctes or name in allowed:
            continue
        raise ExpertQueryError(
            f"Relation {relation!r} is not a governed semantic view. "
            f"Available views: {', '.join(sorted(allowed))}."
        )
    return candidate


def run_expert_query(
    database: Any,
    sql: str,
    *,
    row_limit: int = 2000,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Execute validated SQL read-only with a row cap and a watchdog."""
    wrapped = f"SELECT * FROM (\n{sql}\n) AS expert_result LIMIT {row_limit + 1}"
    started = time.monotonic()
    with database.connect(read_only=True) as db:
        raw = getattr(db, "connection", db)
        timer = threading.Timer(
            timeout_seconds,
            lambda: getattr(raw, "interrupt", lambda: None)(),
        )
        timer.start()
        try:
            cursor = db.execute(wrapped)
            rows = cursor.fetchall()
            columns = [str(item[0]) for item in cursor.description]
        except Exception as error:
            if time.monotonic() - started >= timeout_seconds - 0.5:
                raise ExpertQueryError(
                    f"The query exceeded the {timeout_seconds:.0f}s limit "
                    "and was cancelled. Narrow the time range or add "
                    "aggregation."
                ) from error
            raise ExpertQueryError(f"Query execution failed: {error}") from error
        finally:
            timer.cancel()
    truncated = len(rows) > row_limit
    if truncated:
        rows = rows[:row_limit]
    return {
        "columns": columns,
        "rows": rows,
        "truncated": truncated,
        "rowLimit": row_limit,
        "durationMs": int((time.monotonic() - started) * 1000),
    }
