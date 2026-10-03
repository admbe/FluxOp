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

import logging
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
# Deliberately permissive: this must match whatever sits in relation position,
# not just things that look like identifiers. The previous pattern required a
# leading [a-zA-Z_], so a bare string literal -- which is how DuckDB spells a
# replacement scan, FROM '/home/site/wwwroot/.env' -- matched nothing at all,
# the allowlist loop never executed, and the query was returned unvalidated.
_RELATION_KEYWORD = re.compile(r"(?i)\b(from|join)\b")
_BARE_IDENTIFIER = re.compile(r"[a-zA-Z_]\w*\Z")
_NUMERIC_TOKEN = re.compile(r"[+-]?\d+(?:\.\d*)?\Z")
# Keywords that terminate a comma-separated relation list.
_RELATION_LIST_END = frozenset({
    "where", "group", "having", "order", "limit", "offset", "window",
    "qualify", "union", "except", "intersect", "on", "using", "select",
    "inner", "left", "right", "full", "cross", "natural", "lateral",
    "join", "from",
})


_logger = logging.getLogger(__name__)


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


def _function_call_spans(cleaned: str) -> list[tuple[int, int]]:
    """(open, close) index pairs for parens that open a function call.

    A paren immediately preceded by an identifier character opens a call, so
    any FROM inside it is argument syntax rather than a relation clause.
    Unclosed parens extend to the end of the statement.
    """
    spans: list[tuple[int, int]] = []
    stack: list[tuple[int, bool]] = []
    for index, ch in enumerate(cleaned):
        if ch == "(":
            back = index - 1
            while back >= 0 and cleaned[back].isspace():
                back -= 1
            is_call = back >= 0 and (cleaned[back].isalnum() or cleaned[back] == "_")
            if is_call:
                # `... FROM tbl (` is not a call; a bare keyword before the
                # paren means a grouping or a derived table.
                word_end = back + 1
                word_start = word_end
                while word_start > 0 and (
                    cleaned[word_start - 1].isalnum() or cleaned[word_start - 1] == "_"
                ):
                    word_start -= 1
                if cleaned[word_start:word_end].lower() in _RELATION_LIST_END:
                    is_call = False
            stack.append((index, is_call))
        elif ch == ")" and stack:
            start, is_call = stack.pop()
            if is_call:
                spans.append((start, index))
    while stack:
        start, is_call = stack.pop()
        if is_call:
            spans.append((start, len(cleaned)))
    return spans


def _relations_in(cleaned: str) -> list[str]:
    """Every token sitting in relation position in a literal-stripped query.

    Walks each FROM/JOIN and consumes its comma-separated relation list, so
    ``FROM governed_view, '/etc/passwd'`` surfaces both entries rather than
    only the first. A parenthesised subquery yields nothing -- its own
    FROM/JOIN clauses are visited by later iterations.
    """
    call_spans = _function_call_spans(cleaned)
    relations: list[str] = []
    for match in _RELATION_KEYWORD.finditer(cleaned):
        # extract(month FROM ts), trim(BOTH ' ' FROM x), substring(x FROM 1
        # FOR 3): the FROM is argument syntax, not a relation clause.
        if any(start < match.start() < end for start, end in call_spans):
            continue
        index = match.end()
        while True:
            while index < len(cleaned) and cleaned[index].isspace():
                index += 1
            if index >= len(cleaned):
                break
            if cleaned[index] == "(":
                # A subquery: its own FROM/JOIN clauses are visited by later
                # iterations. But FROM ('<path>') is a parenthesised
                # replacement scan with no inner FROM, so a literal sitting
                # directly inside the parens must still be rejected.
                probe = index + 1
                while probe < len(cleaned) and (
                    cleaned[probe] == "(" or cleaned[probe].isspace()
                ):
                    probe += 1
                if cleaned.startswith("''", probe):
                    relations.append("''")
                break
            start = index
            while index < len(cleaned) and cleaned[index] not in " \t\r\n,;)":
                index += 1
            token = cleaned[start:index]
            if not token:
                break
            if token.lower() in _RELATION_LIST_END:
                break
            if _NUMERIC_TOKEN.match(token):
                # `substring(x FROM 1 FOR 3)` and friends: a relation is
                # never a number, and rejecting these broke legitimate SQL.
                break
            relations.append(token)
            # Consume an optional [AS] alias, then look for a comma that
            # continues the relation list.
            for _ in range(2):
                probe = index
                while probe < len(cleaned) and cleaned[probe] in " \t\r\n":
                    probe += 1
                start_alias = probe
                while probe < len(cleaned) and cleaned[probe] not in " \t\r\n,;)":
                    probe += 1
                alias = cleaned[start_alias:probe]
                if not alias or not _BARE_IDENTIFIER.match(alias):
                    break
                if alias.lower() != "as" and alias.lower() in _RELATION_LIST_END:
                    break
                index = probe
                if alias.lower() != "as":
                    break
            while index < len(cleaned) and cleaned[index] in " \t\r\n":
                index += 1
            if index < len(cleaned) and cleaned[index] == ",":
                index += 1
                continue
            break
    return relations


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
    for relation in _relations_in(cleaned):
        name = relation.strip().strip('"').lower()
        if not _BARE_IDENTIFIER.match(name):
            # Covers the replacement-scan bypass (FROM '<path>', which the
            # literal stripper leaves as FROM '') and anything else that is
            # not a plain identifier.
            raise ExpertQueryError(
                f"Relation {relation!r} is not a governed semantic view; "
                "only bare view names are queryable."
            )
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
        # NOTE: DuckDB treats enable_external_access as a start-up-only
        # global, so it cannot be turned off here on a live connection. The
        # engine-level control belongs in the connection config at open time
        # (api/database.py::_open_connection); until then validate_expert_sql
        # is the only thing standing between a generated query and a
        # replacement scan, which is why it rejects everything that is not a
        # bare allowlisted identifier in relation position.
        timer = threading.Timer(
            timeout_seconds,
            lambda: getattr(raw, "interrupt", lambda: None)(),
        )
        timer.start()
        try:
            cursor = db.execute(wrapped)
            rows = cursor.fetchall()
            columns = [str(item[0]) for item in cursor.description]
            # DuckDB reports full SQL type names (DATE, TIMESTAMP, DECIMAL(10,2),
            # VARCHAR, ...). Callers use them to pick chart encodings without
            # guessing from cell values.
            types = [str(item[1]) for item in cursor.description]
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
        "types": types,
        "rows": rows,
        "truncated": truncated,
        "rowLimit": row_limit,
        "durationMs": int((time.monotonic() - started) * 1000),
    }
