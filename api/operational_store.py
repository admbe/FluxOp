from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import queue
import re
import json
import threading
import time
from typing import Any, Iterator
from uuid import uuid4
import ssl

import duckdb
from filelock import FileLock, Timeout as _FileLockTimeout
import pg8000.dbapi
from sqlalchemy.engine import make_url


def _translate_sql(sql: str, backend: str) -> str:
    if backend != "postgres":
        return sql
    translated = sql.replace("?", "%s")
    # DuckDB accepts ``DOUBLE`` while PostgreSQL accepts both ``DOUBLE`` and
    # ``DOUBLE PRECISION``. Keep an already-qualified declaration intact;
    # replacing every token would produce invalid ``DOUBLE PRECISION PRECISION``.
    return re.sub(
        r"\bDOUBLE\b(?!\s+PRECISION)",
        "DOUBLE PRECISION",
        translated,
    )


@dataclass(slots=True)
class _ConnectionAdapter:
    connection: Any
    backend: str
    read_only: bool = False
    _cursor: Any | None = None

    def execute(self, sql: str, params: Any | None = None) -> "_ConnectionAdapter":
        translated = _translate_sql(sql, self.backend)
        arguments = [] if params is None else params
        if self.backend == "duckdb":
            if params is None:
                self._cursor = self.connection.execute(translated)
            else:
                self._cursor = self.connection.execute(translated, arguments)
        else:
            self._cursor = self.connection.cursor()
            self._cursor.execute(translated, arguments)
        return self

    def executemany(
        self, sql: str, params: list[tuple[Any, ...]] | list[list[Any]]
    ) -> "_ConnectionAdapter":
        translated = _translate_sql(sql, self.backend)
        if self.backend == "duckdb":
            self._cursor = self.connection.executemany(translated, params)
        else:
            self._cursor = self.connection.cursor()
            self._cursor.executemany(translated, params)
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._cursor is None:
            return None
        return self._cursor.fetchone()

    def fetchall(self) -> list[tuple[Any, ...]]:
        if self._cursor is None:
            return []
        return list(self._cursor.fetchall())

    def commit(self) -> None:
        if self.backend != "duckdb":
            self.connection.commit()

    def rollback(self) -> None:
        if self.backend != "duckdb":
            self.connection.rollback()

    def close(self) -> None:
        self.connection.close()


class OperationalPoolTimeout(RuntimeError):
    """No pooled PostgreSQL connection became available in time."""


# One well-known name for the mutable analytical database's global lease.
# Every process that opens it for writing must contend on this exact key.
_WRITER_LEASE_KEY = "flux-duckdb-writer"
_WRITER_LEASE_POLL_SECONDS = 0.25


class WriterLeaseTimeout(RuntimeError):
    """The cross-instance DuckDB writer lease was not obtained in time."""

    def __init__(self, waited: float):
        self.waited = waited
        super().__init__(
            f"The analytical writer lease was held elsewhere for {waited:.1f}s."
        )


class SingletonLeaseUnavailable(RuntimeError):
    """Another process already holds the named cross-process lease."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"The singleton lease '{name}' is held elsewhere.")


class OperationalStore:
    _DEFAULT_POOL_SIZE = 8

    def __init__(
        self,
        *,
        database_url: str = "",
        duckdb_path: Path,
        default_azure_provider: str = "local_powershell",
    ) -> None:
        self.database_url = database_url.strip()
        self.duckdb_path = Path(duckdb_path)
        self.default_azure_provider = default_azure_provider
        self.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
        # PostgreSQL connection pool. A pooled connection avoids a TLS + auth
        # round-trip on every read; without one, the operational control plane
        # opens and tears down a fresh socket per call and the dashboard pays
        # the full handshake on every page load and poll tick. The pool uses a
        # bounded semaphore for capacity and a LIFO queue for idle connections.
        self._pool_size = max(
            1, int(os.getenv("FLUX_OPERATIONAL_POOL_SIZE", str(self._DEFAULT_POOL_SIZE)))
        )
        self._capacity = threading.BoundedSemaphore(self._pool_size)
        self._acquire_timeout = float(
            os.getenv("FLUX_OPERATIONAL_POOL_TIMEOUT_SECONDS", "30")
        )
        self._idle: queue.LifoQueue[Any] = queue.LifoQueue(maxsize=self._pool_size)
        self._closed = False

    def _new_connection(self) -> Any:
        url = make_url(self.database_url)
        ssl_mode = str(url.query.get("sslmode", "require")).lower()
        ssl_context: ssl.SSLContext | bool = (
            False if ssl_mode == "disable" else ssl.create_default_context()
        )
        return pg8000.dbapi.connect(
            user=url.username or "",
            password=url.password or "",
            host=url.host or "127.0.0.1",
            port=url.port or 5432,
            database=url.database or "",
            ssl_context=ssl_context,
            timeout=30,
            application_name="FluxFinOps",
        )

    def _ping(self, connection: Any) -> bool:
        """Return True when an idle pooled connection is still usable."""
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            return True
        except Exception:
            return False

    def _acquire(self) -> Any:
        # The semaphore counts in-use connections only; every checkout takes
        # one permit and every checkin returns it. Blocking here applies
        # backpressure instead of opening unbounded sockets, but never waits
        # forever: a wedged pool must fail loudly, not hang the process.
        if not self._capacity.acquire(timeout=self._acquire_timeout):
            raise OperationalPoolTimeout(
                "No PostgreSQL pool connection became available within "
                f"{self._acquire_timeout:.0f}s; "
                f"{self._pool_size} connections are checked out."
            )
        try:
            try:
                connection = self._idle.get_nowait()
            except queue.Empty:
                return self._new_connection()
            if self._ping(connection):
                return connection
            # The idle connection died (server timeout, restart, etc.).
            try:
                connection.close()
            except Exception:
                pass
            return self._new_connection()
        except OperationalPoolTimeout:
            raise
        except BaseException:
            # The checkout failed (connection error); the permit must not leak.
            self._capacity.release()
            raise

    def _release(self, connection: Any, *, healthy: bool) -> None:
        try:
            if healthy:
                try:
                    self._idle.put_nowait(connection)
                    return
                except queue.Full:
                    pass
            try:
                connection.close()
            except Exception:
                pass
        finally:
            self._capacity.release()

    def close(self) -> None:
        """Close every idle pooled connection (clean shutdown / tests).

        Idle connections hold no capacity permit, so closing them releases
        nothing; permits belong exclusively to checked-out connections.
        """
        self._closed = True
        while True:
            try:
                connection = self._idle.get_nowait()
            except queue.Empty:
                break
            try:
                connection.close()
            except Exception:
                pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @property
    def backend(self) -> str:
        return "postgres" if self.database_url else "duckdb"

    @contextmanager
    def duckdb_writer_lease(self, timeout: float = -1.0) -> Iterator[None]:
        """Hold the cross-INSTANCE lease for the mutable analytical database.

        FluxDatabase guards DuckDB with a FileLock, which uses flock(). On App
        Service, /home is a CIFS mount and flock() there is client-local: it
        does not coordinate the instances sharing that mount. Proven
        2026-07-31 by holding the lease on one instance and acquiring it
        simultaneously on the other -- so two processes wrote the same DuckDB
        file concurrently, which is what produced a week of torn-page
        corruption (RLE segments, dictionary strings, ART indexes).

        A PostgreSQL advisory lock is genuinely global, and being
        session-scoped it is released automatically if the holder dies, so a
        crashed writer cannot wedge the database. Polls rather than using
        blocking pg_advisory_lock so the wait stays bounded and interruptible.

        timeout < 0 waits indefinitely (worker and CLI); a positive timeout
        raises WriterLeaseTimeout so the web can fail fast with 503.
        """
        if not self.database_url:
            # Development runs one process on one machine, where the caller's
            # file lock is a real lock. Nothing further to coordinate.
            yield
            return

        started = time.monotonic()
        deadline = None if timeout < 0 else started + max(0.0, timeout)
        connection = None
        held = False
        try:
            while True:
                # Take a pooled connection only for the attempt itself. A
                # waiter that camped on one would starve the pool (8 slots)
                # while doing nothing, turning a slow writer into an outage.
                connection = self._acquire()
                cursor = connection.cursor()
                cursor.execute(
                    "SELECT pg_try_advisory_lock(hashtext(%s))",
                    (_WRITER_LEASE_KEY,),
                )
                held = bool(cursor.fetchone()[0])
                cursor.close()
                connection.commit()
                if held:
                    break
                self._release(connection, healthy=True)
                connection = None
                if deadline is not None and time.monotonic() >= deadline:
                    raise WriterLeaseTimeout(time.monotonic() - started)
                time.sleep(_WRITER_LEASE_POLL_SECONDS)
            yield
        finally:
            # A timeout leaves no connection checked out; only release one we
            # still hold.
            if connection is not None:
                healthy = True
                if held:
                    try:
                        cursor = connection.cursor()
                        cursor.execute(
                            "SELECT pg_advisory_unlock(hashtext(%s))",
                            (_WRITER_LEASE_KEY,),
                        )
                        cursor.close()
                        connection.commit()
                    except Exception:
                        # An un-released session lock would outlive the pooled
                        # connection and wedge every future writer; discard it.
                        healthy = False
                self._release(connection, healthy=healthy)

    @contextmanager
    def singleton_lease(self, name: str) -> Iterator[None]:
        """Hold a cross-process singleton lease for the named job.

        PostgreSQL backend: a session-scoped advisory lock on a pooled
        connection held for the lease lifetime — it disappears automatically
        if the holding process dies, so a crashed job can never wedge its
        successor. DuckDB backend (development): the historical non-blocking
        file lock. Raises SingletonLeaseUnavailable when another process
        holds the lease.
        """
        if not self.database_url:
            try:
                with FileLock(str(self.duckdb_path) + f".{name}.lock", timeout=0):
                    yield
                return
            except _FileLockTimeout:
                raise SingletonLeaseUnavailable(name) from None

        connection = self._acquire()
        held = False
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))", (name,)
            )
            held = bool(cursor.fetchone()[0])
            cursor.close()
            connection.commit()
            if not held:
                raise SingletonLeaseUnavailable(name)
            yield
        finally:
            healthy = True
            if held:
                try:
                    cursor = connection.cursor()
                    cursor.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s))", (name,)
                    )
                    cursor.close()
                    connection.commit()
                except Exception:
                    # The unlock failed; the session lock would outlive the
                    # pooled connection, so the connection must be discarded.
                    healthy = False
            self._release(connection, healthy=healthy)

    @contextmanager
    def connect(self, read_only: bool = False) -> Iterator[_ConnectionAdapter]:
        if self.database_url:
            connection = self._acquire()
            adapter = _ConnectionAdapter(
                connection=connection,
                backend="postgres",
                read_only=read_only,
            )
            healthy = True
            try:
                yield adapter
                if not read_only:
                    adapter.commit()
                else:
                    # End the implicit read transaction so the returned pooled
                    # connection is clean for the next caller.
                    try:
                        adapter.rollback()
                    except Exception:
                        pass
            except Exception:
                healthy = False
                try:
                    adapter.rollback()
                except Exception:
                    pass
                raise
            finally:
                self._release(connection, healthy=healthy)
            return

        connection = duckdb.connect(str(self.duckdb_path), read_only=read_only)
        adapter = _ConnectionAdapter(connection=connection, backend="duckdb", read_only=read_only)
        try:
            yield adapter
        finally:
            adapter.close()

    def claim_throttle_slot(self, name: str, interval_seconds: float) -> float:
        """Atomically claim the next shared request slot for ``name``.

        Returns 0 when the slot was claimed; otherwise the number of seconds
        to wait before retrying. State is shared through the operational
        store, so every process observes the same request spacing (the
        replacement for the historical file-based rate gate).
        """
        now = datetime.now(timezone.utc)
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO throttle_state (name, next_allowed_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (name) DO NOTHING
                """,
                [name, now, now],
            )
            claimed = db.execute(
                """
                UPDATE throttle_state
                SET next_allowed_at = ?, updated_at = ?
                WHERE name = ? AND next_allowed_at <= ?
                RETURNING name
                """,
                [now + timedelta(seconds=interval_seconds), now, name, now],
            ).fetchone()
            if claimed:
                return 0.0
            pending = db.execute(
                "SELECT next_allowed_at FROM throttle_state WHERE name = ?",
                [name],
            ).fetchone()
        if not pending or pending[0] is None:
            return 0.5
        next_allowed = pending[0]
        if isinstance(next_allowed, str):
            next_allowed = datetime.fromisoformat(next_allowed.replace("Z", "+00:00"))
        if next_allowed.tzinfo is None:
            next_allowed = next_allowed.replace(tzinfo=timezone.utc)
        return max(0.05, (next_allowed - now).total_seconds())

    def register_throttle_cooldown(self, name: str, cooldown_seconds: float) -> None:
        """Push the shared next-allowed time forward after an upstream 429."""
        now = datetime.now(timezone.utc)
        blocked_until = now + timedelta(seconds=max(0.0, cooldown_seconds))
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO throttle_state (name, next_allowed_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (name) DO NOTHING
                """,
                [name, blocked_until, now],
            )
            db.execute(
                """
                UPDATE throttle_state
                SET next_allowed_at = CASE
                        WHEN next_allowed_at > ? THEN next_allowed_at
                        ELSE ?
                    END,
                    updated_at = ?
                WHERE name = ?
                """,
                [blocked_until, blocked_until, now, name],
            )

    def claim_cost_management_quota(
        self,
        name: str,
        estimated_qpu: float,
        windows: list[tuple[int, float]],
        *,
        minimum_interval_seconds: float = 0.0,
    ) -> tuple[float, str | None]:
        """Reserve capacity in durable rolling QPU windows.

        ``windows`` contains ``(window_seconds, budget_qpu)`` pairs. Active
        reservations are kept in the state row so separate workers sharing
        PostgreSQL (or the operational DuckDB) see the same budget. The row is
        updated before it is read, providing the database lock that serializes
        competing reservations. A positive return value is the wait required;
        a zero wait returns the reservation id.
        """
        requested = max(float(estimated_qpu), 1.0)
        normalized_windows = [
            (max(int(seconds), 1), max(float(budget), requested))
            for seconds, budget in windows
        ]
        max_window = max((seconds for seconds, _ in normalized_windows), default=1)
        now = datetime.now(timezone.utc)
        state_name = str(name or "cost-management")
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO cost_management_quota_state (
                    name, next_allowed_at, cooldown_until,
                    reservations_json, updated_at
                ) VALUES (?, ?, ?, '[]', ?)
                ON CONFLICT (name) DO NOTHING
                """,
                [state_name, now, now, now],
            )
            # UPDATE obtains a row lock on PostgreSQL and serializes state
            # changes on DuckDB's transactional store.
            db.execute(
                """
                UPDATE cost_management_quota_state
                SET updated_at = ?
                WHERE name = ?
                """,
                [now, state_name],
            )
            row = db.execute(
                """
                SELECT next_allowed_at, cooldown_until, reservations_json
                FROM cost_management_quota_state
                WHERE name = ?
                """,
                [state_name],
            ).fetchone()
            if not row:
                return 0.5, None
            next_allowed, cooldown_until, raw_reservations = row
            try:
                reservations = json.loads(raw_reservations or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                reservations = []
            active: list[dict[str, Any]] = []
            for item in reservations if isinstance(reservations, list) else []:
                try:
                    reserved_at = datetime.fromisoformat(
                        str(item["reservedAt"]).replace("Z", "+00:00")
                    )
                    if reserved_at.tzinfo is None:
                        reserved_at = reserved_at.replace(tzinfo=timezone.utc)
                    if (now - reserved_at).total_seconds() < max_window:
                        active.append(
                            {
                                "id": str(item["id"]),
                                "reservedAt": reserved_at.isoformat(),
                                "qpu": max(float(item.get("qpu", 1)), 1.0),
                            }
                        )
                except (KeyError, TypeError, ValueError):
                    continue

            def as_datetime(value: Any) -> datetime:
                if isinstance(value, datetime):
                    result = value
                else:
                    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                return result if result.tzinfo else result.replace(tzinfo=timezone.utc)

            wait_until = max(as_datetime(next_allowed), as_datetime(cooldown_until))
            for window_seconds, budget in normalized_windows:
                recent = [
                    item
                    for item in active
                    if (now - datetime.fromisoformat(item["reservedAt"])).total_seconds()
                    < window_seconds
                ]
                total = sum(float(item["qpu"]) for item in recent)
                if total + requested <= budget:
                    continue
                running = total
                for item in sorted(recent, key=lambda entry: entry["reservedAt"]):
                    running -= float(item["qpu"])
                    expiry = (
                        datetime.fromisoformat(item["reservedAt"])
                        + timedelta(seconds=window_seconds)
                    )
                    if running + requested <= budget:
                        wait_until = max(wait_until, expiry)
                        break

            delay = max(0.0, (wait_until - now).total_seconds())
            if delay > 0:
                db.execute(
                    """
                    UPDATE cost_management_quota_state
                    SET reservations_json = ?, updated_at = ?
                    WHERE name = ?
                    """,
                    [json.dumps(active, separators=(",", ":")), now, state_name],
                )
                return delay, None

            reservation_id = str(uuid4())
            active.append(
                {
                    "id": reservation_id,
                    "reservedAt": now.isoformat(),
                    "qpu": requested,
                }
            )
            db.execute(
                """
                UPDATE cost_management_quota_state
                SET next_allowed_at = ?,
                    reservations_json = ?, updated_at = ?
                WHERE name = ?
                """,
                [
                    now + timedelta(seconds=max(0.0, minimum_interval_seconds)),
                    json.dumps(active, separators=(",", ":")),
                    now,
                    state_name,
                ],
            )
            return 0.0, reservation_id

    def reconcile_cost_management_quota(
        self,
        name: str,
        reservation_id: str | None,
        *,
        consumed_qpu: float | None = None,
    ) -> None:
        """Reconcile a reservation with Azure's consumed-QPU response header."""
        if not reservation_id or consumed_qpu is None:
            return
        state_name = str(name or "cost-management")
        with self.connect() as db:
            row = db.execute(
                """
                SELECT reservations_json FROM cost_management_quota_state
                WHERE name = ?
                """,
                [state_name],
            ).fetchone()
            if not row:
                return
            try:
                reservations = json.loads(row[0] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                return
            changed = False
            for item in reservations if isinstance(reservations, list) else []:
                if str(item.get("id")) == reservation_id:
                    item["qpu"] = max(float(consumed_qpu), 1.0)
                    changed = True
                    break
            if changed:
                db.execute(
                    """
                    UPDATE cost_management_quota_state
                    SET reservations_json = ?, updated_at = ?
                    WHERE name = ?
                    """,
                    [json.dumps(reservations, separators=(",", ":")), datetime.now(timezone.utc), state_name],
                )

    def register_cost_management_quota_cooldown(
        self, name: str, cooldown_seconds: float
    ) -> None:
        now = datetime.now(timezone.utc)
        blocked_until = now + timedelta(seconds=max(0.0, cooldown_seconds))
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO cost_management_quota_state (
                    name, next_allowed_at, cooldown_until,
                    reservations_json, updated_at
                ) VALUES (?, ?, ?, '[]', ?)
                ON CONFLICT (name) DO NOTHING
                """,
                [name, blocked_until, blocked_until, now],
            )
            db.execute(
                """
                UPDATE cost_management_quota_state
                SET cooldown_until = CASE
                        WHEN cooldown_until > ? THEN cooldown_until
                        ELSE ?
                    END,
                    next_allowed_at = CASE
                        WHEN next_allowed_at > ? THEN next_allowed_at
                        ELSE ?
                    END,
                    updated_at = ?
                WHERE name = ?
                """,
                [
                    blocked_until,
                    blocked_until,
                    blocked_until,
                    blocked_until,
                    now,
                    name,
                ],
            )

    def init(self) -> None:
        with self.connect() as db:
            # Serialize schema init across processes. Every job and the web
            # replay this DDL on startup; two processes running the ALTERs
            # concurrently deadlock PostgreSQL on AccessExclusiveLock
            # (40P01) -- flux-cost crashed exactly this way at its
            # 2026-08-01 11:00 UTC schedule, colliding with another
            # process's startup, and the whole day's cost collection was
            # lost. init runs as one transaction (connect() commits or
            # rolls back on exit), so a transaction-scoped advisory lock
            # serializes the DDL and releases itself on either outcome --
            # it cannot leak onto the pooled session.
            if db.backend == "postgres":
                db.execute(
                    "SELECT pg_advisory_xact_lock("
                    "hashtext('flux-operational-schema-init'))"
                )
            script = """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL
                );

                CREATE TABLE IF NOT EXISTS azure_integration (
                    id VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    tenant_id VARCHAR,
                    enabled BOOLEAN NOT NULL,
                    auth_mode VARCHAR NOT NULL,
                    subscriptions_json TEXT NOT NULL,
                    last_sync_at TIMESTAMPTZ,
                    last_sync_status VARCHAR NOT NULL,
                    last_sync_message VARCHAR NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_runs (
                    id VARCHAR PRIMARY KEY,
                    provider VARCHAR NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL,
                    completed_at TIMESTAMPTZ,
                    status VARCHAR NOT NULL,
                    resource_count INTEGER NOT NULL DEFAULT 0,
                    message VARCHAR NOT NULL DEFAULT '',
                    trigger VARCHAR NOT NULL DEFAULT 'manual',
                    stage VARCHAR NOT NULL DEFAULT '',
                    stage_message VARCHAR NOT NULL DEFAULT '',
                    claimed_at TIMESTAMPTZ,
                    claimed_by VARCHAR,
                    claim_expires_at TIMESTAMPTZ,
                    requested_sources_json TEXT NOT NULL DEFAULT
                        '["inventory","advisor","intelligence","policy","cost"]'
                );

                CREATE TABLE IF NOT EXISTS throttle_state (
                    name VARCHAR PRIMARY KEY,
                    next_allowed_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cost_management_quota_state (
                    name VARCHAR PRIMARY KEY,
                    next_allowed_at TIMESTAMPTZ NOT NULL,
                    cooldown_until TIMESTAMPTZ NOT NULL,
                    reservations_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TIMESTAMPTZ NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_source_runs (
                    sync_id VARCHAR NOT NULL,
                    source VARCHAR NOT NULL,
                    scope_id VARCHAR NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL,
                    completed_at TIMESTAMPTZ,
                    status VARCHAR NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    retained_last_good BOOLEAN NOT NULL DEFAULT FALSE,
                    message VARCHAR NOT NULL DEFAULT '',
                    last_attempt_at TIMESTAMPTZ,
                    status_code INTEGER,
                    retry_after_seconds DOUBLE PRECISION,
                    qpu_consumed DOUBLE PRECISION,
                    qpu_remaining DOUBLE PRECISION,
                    next_retry_at TIMESTAMPTZ
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_source_run
                    ON sync_source_runs(sync_id, source, scope_id);

                CREATE TABLE IF NOT EXISTS focus_import_runs (
                    run_id VARCHAR PRIMARY KEY,
                    started_at TIMESTAMPTZ NOT NULL,
                    completed_at TIMESTAMPTZ,
                    status VARCHAR NOT NULL,
                    manifest_count INTEGER NOT NULL DEFAULT 0,
                    charge_count INTEGER NOT NULL DEFAULT 0,
                    message VARCHAR NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS intelligence_usage_events (
                    request_id VARCHAR PRIMARY KEY,
                    occurred_at TIMESTAMPTZ NOT NULL,
                    user_hash VARCHAR NOT NULL,
                    provider VARCHAR NOT NULL,
                    model VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    prompt_tokens BIGINT NOT NULL DEFAULT 0,
                    cached_prompt_tokens BIGINT NOT NULL DEFAULT 0,
                    completion_tokens BIGINT NOT NULL DEFAULT 0,
                    estimated_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
                    tool_names_json TEXT NOT NULL DEFAULT '[]',
                    tool_call_count INTEGER NOT NULL DEFAULT 0,
                    error_code VARCHAR NOT NULL DEFAULT '',
                    feedback_rating VARCHAR NOT NULL DEFAULT '',
                    feedback_reason VARCHAR NOT NULL DEFAULT '',
                    feedback_at TIMESTAMPTZ,
                    model_latency_ms INTEGER NOT NULL DEFAULT 0,
                    governed_tool_latency_ms INTEGER NOT NULL DEFAULT 0,
                    database_latency_ms INTEGER NOT NULL DEFAULT 0,
                    validation_latency_ms INTEGER NOT NULL DEFAULT 0,
                    application_latency_ms INTEGER NOT NULL DEFAULT 0,
                    model_call_count INTEGER NOT NULL DEFAULT 0,
                    tool_latency_json TEXT NOT NULL DEFAULT '[]',
                    client_round_trip_ms INTEGER,
                    client_render_ms INTEGER,
                    client_end_to_end_ms INTEGER,
                    transport_ingress_ms INTEGER
                );

                CREATE TABLE IF NOT EXISTS intelligence_transcript_events (
                    request_id VARCHAR PRIMARY KEY,
                    occurred_at TIMESTAMPTZ NOT NULL,
                    user_hash VARCHAR NOT NULL,
                    messages_json TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    response_json TEXT,
                    raw_response_text VARCHAR NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_intelligence_transcript_occurred
                    ON intelligence_transcript_events(occurred_at);

                CREATE TABLE IF NOT EXISTS cost_anomaly_reviews (
                    run_id VARCHAR NOT NULL,
                    cost_type VARCHAR NOT NULL,
                    scope_type VARCHAR NOT NULL,
                    scope_id VARCHAR NOT NULL,
                    review_status VARCHAR NOT NULL,
                    note VARCHAR NOT NULL DEFAULT '',
                    updated_by VARCHAR NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (run_id, cost_type, scope_type, scope_id)
                );

                CREATE TABLE IF NOT EXISTS source_sync_state (
                    snapshot_id VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    source VARCHAR NOT NULL,
                    scope_id VARCHAR NOT NULL,
                    row_count INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cost_history_runs (
                    run_id VARCHAR PRIMARY KEY,
                    started_at TIMESTAMPTZ NOT NULL,
                    completed_at TIMESTAMPTZ,
                    status VARCHAR NOT NULL,
                    expected_scopes INTEGER NOT NULL DEFAULT 0,
                    completed_scopes INTEGER NOT NULL DEFAULT 0,
                    failed_scopes INTEGER NOT NULL DEFAULT 0,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    message VARCHAR NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS cost_history_scope_runs (
                    run_id VARCHAR NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    cost_type VARCHAR NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL,
                    completed_at TIMESTAMPTZ,
                    status VARCHAR NOT NULL,
                    query_start DATE NOT NULL,
                    query_end DATE NOT NULL,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    retained_last_good BOOLEAN NOT NULL DEFAULT FALSE,
                    status_code INTEGER,
                    message VARCHAR NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_cost_history_scope_run
                    ON cost_history_scope_runs(run_id, subscription_id, cost_type);

                CREATE TABLE IF NOT EXISTS cost_history_request_attempts (
                    attempt_id VARCHAR PRIMARY KEY,
                    run_id VARCHAR NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    cost_type VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    status VARCHAR NOT NULL,
                    status_code INTEGER,
                    retry_after_seconds DOUBLE PRECISION,
                    qpu_consumed DOUBLE PRECISION,
                    qpu_remaining DOUBLE PRECISION,
                    message VARCHAR NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_cost_history_request_scope
                    ON cost_history_request_attempts(
                        run_id, subscription_id, cost_type
                    );

                CREATE TABLE IF NOT EXISTS cost_details_backfill_scopes (
                    subscription_id VARCHAR NOT NULL,
                    cost_type VARCHAR NOT NULL,
                    period_start DATE NOT NULL,
                    period_end DATE NOT NULL,
                    status VARCHAR NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    first_attempt_at TIMESTAMPTZ,
                    last_attempt_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ,
                    next_retry_at TIMESTAMPTZ,
                    status_code INTEGER,
                    message VARCHAR NOT NULL DEFAULT '',
                    source VARCHAR NOT NULL DEFAULT 'azure_cost_details_report'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_cost_details_backfill_status
                    ON cost_details_backfill_scopes(
                        subscription_id, cost_type, period_start
                    );
                CREATE TABLE IF NOT EXISTS budget_targets (
                    scope_type VARCHAR NOT NULL,
                    scope_id VARCHAR NOT NULL DEFAULT '',
                    monthly_amount DOUBLE NOT NULL,
                    currency VARCHAR NOT NULL DEFAULT 'USD',
                    updated_by VARCHAR NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (scope_type, scope_id)
                );

                CREATE TABLE IF NOT EXISTS budget_groups (
                    id VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    annual_amount DOUBLE NOT NULL,
                    currency VARCHAR NOT NULL DEFAULT 'USD',
                    position INTEGER NOT NULL DEFAULT 0,
                    updated_by VARCHAR NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL
                );

                CREATE TABLE IF NOT EXISTS budget_group_members (
                    group_id VARCHAR NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    PRIMARY KEY (group_id, subscription_id)
                );

                CREATE TABLE IF NOT EXISTS opportunity_lifecycle (
                    opportunity_id VARCHAR PRIMARY KEY,
                    status VARCHAR NOT NULL DEFAULT 'open',
                    note VARCHAR NOT NULL DEFAULT '',
                    updated_by VARCHAR NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL,
                    implemented_at TIMESTAMPTZ,
                    resource_id VARCHAR NOT NULL DEFAULT '',
                    estimated_monthly_savings DOUBLE,
                    baseline_monthly_cost DOUBLE
                );

                CREATE TABLE IF NOT EXISTS allocation_config (
                    id VARCHAR PRIMARY KEY,
                    cost_center_tags VARCHAR NOT NULL DEFAULT '[]',
                    shared_values VARCHAR NOT NULL DEFAULT '[]',
                    unit_tag VARCHAR NOT NULL DEFAULT '',
                    unit_label VARCHAR NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_intelligence_config (
                    id VARCHAR PRIMARY KEY,
                    provider VARCHAR NOT NULL DEFAULT '',
                    fast_model VARCHAR NOT NULL DEFAULT '',
                    deep_model VARCHAR NOT NULL DEFAULT '',
                    updated_by VARCHAR NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL
                );

                CREATE TABLE IF NOT EXISTS fiscal_outlook_config (
                    id VARCHAR PRIMARY KEY,
                    fy_start_month INTEGER NOT NULL DEFAULT 7,
                    cost_type VARCHAR NOT NULL DEFAULT 'AmortizedCost',
                    growth_percent_monthly DOUBLE NOT NULL DEFAULT 0,
                    include_planned_savings BOOLEAN NOT NULL DEFAULT FALSE,
                    savings_ramp_months INTEGER NOT NULL DEFAULT 3,
                    notes VARCHAR NOT NULL DEFAULT '',
                    updated_by VARCHAR NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL
                );

                -- Outbound-alert dedupe: one row per currently-active
                -- pipeline warning so the flux-alerts job notifies new
                -- conditions once and persistent ones on a slow cadence.
                CREATE TABLE IF NOT EXISTS pipeline_alert_state (
                    warning_key VARCHAR PRIMARY KEY,
                    first_seen TIMESTAMPTZ NOT NULL,
                    last_notified TIMESTAMPTZ NOT NULL
                );

                -- Virtual tags: Flux-side metadata layered over native Azure
                -- tags. Rules are versioned with an append-only audit trail;
                -- overrides carry per-resource manual/imported values.
                CREATE TABLE IF NOT EXISTS virtual_tag_dimensions (
                    dimension_key VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    description VARCHAR NOT NULL DEFAULT '',
                    status VARCHAR NOT NULL DEFAULT 'active',
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_by VARCHAR NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL
                );

                CREATE TABLE IF NOT EXISTS virtual_tag_rules (
                    rule_id VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    tag_key VARCHAR NOT NULL,
                    tag_value VARCHAR NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    conditions_json TEXT NOT NULL,
                    effect VARCHAR NOT NULL DEFAULT 'include',
                    status VARCHAR NOT NULL DEFAULT 'active',
                    effective_from DATE,
                    effective_to DATE,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_by VARCHAR NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL
                );

                CREATE TABLE IF NOT EXISTS virtual_tag_rule_audit (
                    rule_id VARCHAR NOT NULL,
                    version INTEGER NOT NULL,
                    action VARCHAR NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    actor VARCHAR NOT NULL DEFAULT '',
                    occurred_at TIMESTAMPTZ NOT NULL
                );

                -- Planned-remediation lifecycle: one row per (signal,
                -- resource) remediation cycle. The correlation key is the
                -- ServiceNow-facing identity and the duplicate guard.
                -- NOTE: this init script splits statements on semicolons,
                -- so comments must never contain one mid-line.
                CREATE TABLE IF NOT EXISTS remediation_tasks (
                    correlation_key VARCHAR PRIMARY KEY,
                    signal_kind VARCHAR NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    status VARCHAR NOT NULL DEFAULT 'exported',
                    task_number VARCHAR NOT NULL DEFAULT '',
                    note VARCHAR NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                );

                CREATE TABLE IF NOT EXISTS virtual_tag_overrides (
                    resource_id VARCHAR NOT NULL,
                    tag_key VARCHAR NOT NULL,
                    tag_value VARCHAR NOT NULL,
                    source VARCHAR NOT NULL DEFAULT 'manual',
                    note VARCHAR NOT NULL DEFAULT '',
                    updated_by VARCHAR NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (resource_id, tag_key)
                );

                -- SLO state machine: one row per objective currently in a
                -- non-ok state, so transitions (enter, worsen, recover)
                -- notify exactly once and persistence re-notifies on a slow
                -- cadence. Recovered objectives drop their row.
                CREATE TABLE IF NOT EXISTS slo_state (
                    slo_key VARCHAR PRIMARY KEY,
                    state VARCHAR NOT NULL,
                    since TIMESTAMPTZ NOT NULL,
                    last_notified TIMESTAMPTZ NOT NULL
                );

                -- Right-sizing purchase plan: region+SKU commitment buckets,
                -- per-VM assignments, and an append-only decision log. Lives
                -- in the operational store (not the analytical snapshot) so
                -- the plan survives snapshot swaps and is shared across
                -- instances and planners. One or more named boards partition
                -- all three tables (a fresh install or a pre-boards upgrade
                -- both resolve to a lazily-created "Default" board -- see
                -- FluxDatabase._resolve_rightsizing_board). Exactly one board
                -- is_primary at a time, and the fiscal outlook and the
                -- resource evidence dossier only ever look at the primary
                -- board, so a scratch/exploration board never silently
                -- inflates them.
                CREATE TABLE IF NOT EXISTS rightsizing_boards (
                    id VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    description VARCHAR NOT NULL DEFAULT '',
                    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
                    created_by VARCHAR NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                );

                -- bucket_key is "{board_id}:{region}|{sku}" -- globally
                -- unique across boards while staying a single-column primary
                -- key, so every existing single-column bucket_key lookup
                -- keeps working unmodified. board_id is also stored plainly
                -- for direct per-board filtering.
                CREATE TABLE IF NOT EXISTS rightsizing_plan_buckets (
                    bucket_key VARCHAR PRIMARY KEY,
                    board_id VARCHAR NOT NULL DEFAULT '',
                    region VARCHAR NOT NULL,
                    sku VARCHAR NOT NULL,
                    strategy VARCHAR NOT NULL DEFAULT '',
                    source VARCHAR NOT NULL DEFAULT '',
                    ref_quantity INTEGER,
                    ref_monthly_payg DOUBLE,
                    ref_monthly_ri_1y DOUBLE,
                    ref_ri_1y_upfront DOUBLE,
                    ref_monthly_sp_1y DOUBLE,
                    ref_monthly_savings DOUBLE,
                    ref_reservation_check VARCHAR NOT NULL DEFAULT '',
                    note VARCHAR NOT NULL DEFAULT '',
                    created_by VARCHAR NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                );

                -- vm_key stays the raw (lowercased) Azure resource id -- it is
                -- compared directly against live inventory throughout the
                -- board-assembly code -- so per-board scoping uses a real
                -- composite primary key instead of a prefixed string.
                CREATE TABLE IF NOT EXISTS rightsizing_plan_assignments (
                    board_id VARCHAR NOT NULL DEFAULT '',
                    vm_key VARCHAR NOT NULL,
                    vm_name VARCHAR NOT NULL DEFAULT '',
                    subscription_name VARCHAR NOT NULL DEFAULT '',
                    bucket_key VARCHAR NOT NULL DEFAULT '__unassigned__',
                    decision VARCHAR NOT NULL DEFAULT 'Pending',
                    note VARCHAR NOT NULL DEFAULT '',
                    ref_monthly_payg DOUBLE,
                    ref_monthly_commitment DOUBLE,
                    ref_monthly_savings DOUBLE,
                    economics_status VARCHAR NOT NULL DEFAULT '',
                    source VARCHAR NOT NULL DEFAULT 'ui',
                    updated_by VARCHAR NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (board_id, vm_key)
                );
                ALTER TABLE rightsizing_plan_assignments ADD COLUMN IF NOT EXISTS
                    ref_monthly_payg DOUBLE;
                ALTER TABLE rightsizing_plan_assignments ADD COLUMN IF NOT EXISTS
                    ref_monthly_commitment DOUBLE;
                ALTER TABLE rightsizing_plan_assignments ADD COLUMN IF NOT EXISTS
                    ref_monthly_savings DOUBLE;
                ALTER TABLE rightsizing_plan_assignments ADD COLUMN IF NOT EXISTS
                    economics_status VARCHAR DEFAULT '';

                CREATE TABLE IF NOT EXISTS rightsizing_plan_log (
                    id VARCHAR PRIMARY KEY,
                    board_id VARCHAR NOT NULL DEFAULT '',
                    ts TIMESTAMPTZ NOT NULL,
                    actor VARCHAR NOT NULL DEFAULT '',
                    vm_key VARCHAR NOT NULL DEFAULT '',
                    vm_name VARCHAR NOT NULL DEFAULT '',
                    from_label VARCHAR NOT NULL DEFAULT '',
                    to_label VARCHAR NOT NULL DEFAULT '',
                    decision VARCHAR NOT NULL DEFAULT '',
                    note VARCHAR NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS analytics_apply_jobs (
                    job_id VARCHAR PRIMARY KEY,
                    source VARCHAR NOT NULL,
                    idempotency_key VARCHAR NOT NULL,
                    payload_path VARCHAR NOT NULL,
                    payload_checksum VARCHAR NOT NULL,
                    status VARCHAR NOT NULL DEFAULT 'staged',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    row_counts VARCHAR NOT NULL DEFAULT '{}',
                    error VARCHAR NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL,
                    applied_at TIMESTAMPTZ
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_analytics_apply_jobs_key
                    ON analytics_apply_jobs(idempotency_key);
                CREATE TABLE IF NOT EXISTS analytics_publications (
                    version INTEGER NOT NULL,
                    status VARCHAR NOT NULL,
                    file_name VARCHAR NOT NULL,
                    checksum_sha256 VARCHAR NOT NULL,
                    file_size_bytes BIGINT NOT NULL DEFAULT 0,
                    row_counts VARCHAR NOT NULL DEFAULT '{}',
                    message VARCHAR NOT NULL DEFAULT '',
                    generated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    duration_ms INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_analytics_publications_version
                    ON analytics_publications(version);
                """
            for statement in (part.strip() for part in script.split(";")):
                if statement:
                    db.execute(statement)
            attempt_columns = {
                str(row[0])
                for row in db.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'cost_history_request_attempts'
                    """
                ).fetchall()
            }
            if "request_id" in attempt_columns and "attempt_id" not in attempt_columns:
                db.execute(
                    """
                    ALTER TABLE cost_history_request_attempts
                    RENAME COLUMN request_id TO attempt_id
                    """
                )
            for column in ("qpu_consumed", "qpu_remaining"):
                if column not in attempt_columns:
                    db.execute(
                        f"ALTER TABLE cost_history_request_attempts "
                        f"ADD COLUMN {column} DOUBLE PRECISION"
                    )
            sync_attempt_columns = {
                str(row[0])
                for row in db.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'sync_source_runs'
                    """
                ).fetchall()
            }
            for column in ("qpu_consumed", "qpu_remaining"):
                if column not in sync_attempt_columns:
                    db.execute(
                        f"ALTER TABLE sync_source_runs "
                        f"ADD COLUMN {column} DOUBLE PRECISION"
                    )
            sync_columns = {
                str(row[0])
                for row in db.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'sync_runs'
                    """
                ).fetchall()
            }
            if "claimed_by" not in sync_columns:
                db.execute("ALTER TABLE sync_runs ADD COLUMN claimed_by VARCHAR")
            if "claim_expires_at" not in sync_columns:
                db.execute(
                    "ALTER TABLE sync_runs ADD COLUMN claim_expires_at TIMESTAMPTZ"
                )
            publication_columns = {
                str(row[0])
                for row in db.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'analytics_publications'
                    """
                ).fetchall()
            }
            if publication_columns and "duration_ms" not in publication_columns:
                db.execute(
                    "ALTER TABLE analytics_publications "
                    "ADD COLUMN duration_ms INTEGER NOT NULL DEFAULT 0"
                )
            allocation_columns = {
                str(row[0])
                for row in db.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'allocation_config'
                    """
                ).fetchall()
            }
            if allocation_columns and "unit_tag" not in allocation_columns:
                db.execute(
                    "ALTER TABLE allocation_config "
                    "ADD COLUMN unit_tag VARCHAR NOT NULL DEFAULT ''"
                )
                db.execute(
                    "ALTER TABLE allocation_config "
                    "ADD COLUMN unit_label VARCHAR NOT NULL DEFAULT ''"
                )
            virtual_rule_columns = {
                str(row[0])
                for row in db.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'virtual_tag_rules'
                    """
                ).fetchall()
            }
            if virtual_rule_columns and "effect" not in virtual_rule_columns:
                db.execute(
                    "ALTER TABLE virtual_tag_rules ADD COLUMN effect VARCHAR "
                    "DEFAULT 'include'"
                )
            self._migrate_rightsizing_boards(db)
            if db.backend == "postgres":
                db.execute(
                    """
                    ALTER TABLE cost_details_backfill_scopes
                    ALTER COLUMN attempt_count SET DEFAULT 0
                    """
                )
                db.execute(
                    """
                    ALTER TABLE cost_details_backfill_scopes
                    ALTER COLUMN first_attempt_at DROP NOT NULL
                    """
                )
                db.execute(
                    """
                    ALTER TABLE cost_details_backfill_scopes
                    ALTER COLUMN last_attempt_at DROP NOT NULL
                    """
                )
            existing = db.execute(
                "SELECT count(*) FROM azure_integration"
            ).fetchone()[0]
            if not existing:
                db.execute(
                    """
                    INSERT INTO azure_integration VALUES (
                        'azure', 'Azure', '', TRUE, ?, '[]',
                        NULL, 'never', 'Not synchronized yet.', CURRENT_TIMESTAMP
                    )
                    """,
                    [self.default_azure_provider],
                )

    def _rightsizing_default_board_id(self, db: _ConnectionAdapter) -> str:
        """The primary board id, creating one 'Default' board if none
        exist yet. Idempotent and safe to call more than once per
        transaction (a fresh install and a pre-boards upgrade both land
        here)."""
        row = db.execute(
            "SELECT id FROM rightsizing_boards WHERE is_primary = TRUE "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row:
            return str(row[0])
        row = db.execute(
            "SELECT id FROM rightsizing_boards ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row:
            board_id = str(row[0])
            db.execute(
                "UPDATE rightsizing_boards SET is_primary = TRUE WHERE id = ?",
                [board_id],
            )
            return board_id
        board_id = str(uuid4())
        now = datetime.now(timezone.utc)
        db.execute(
            """
            INSERT INTO rightsizing_boards (
                id, name, description, is_primary, created_by,
                created_at, updated_at
            ) VALUES (?, 'Default', '', TRUE, 'system', ?, ?)
            """,
            [board_id, now, now],
        )
        return board_id

    def _migrate_rightsizing_boards(self, db: _ConnectionAdapter) -> None:
        """Upgrade path for installations that predate boards.

        A fresh install already gets board_id baked into the CREATE TABLE
        statements above, so these checks only fire once per environment,
        the first time this code runs against pre-existing data.
        """
        bucket_columns = {
            str(row[0])
            for row in db.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'rightsizing_plan_buckets'
                """
            ).fetchall()
        }
        if bucket_columns and "board_id" not in bucket_columns:
            default_board_id = self._rightsizing_default_board_id(db)
            db.execute(
                "ALTER TABLE rightsizing_plan_buckets "
                "ADD COLUMN board_id VARCHAR DEFAULT ''"
            )
            db.execute(
                "ALTER TABLE rightsizing_plan_buckets "
                "ADD COLUMN note VARCHAR DEFAULT ''"
            )
            # bucket_key becomes globally unique via a board_id prefix;
            # existing rows are rewritten in place so the single-column
            # PRIMARY KEY constraint never has to change shape.
            db.execute(
                """
                UPDATE rightsizing_plan_buckets
                SET board_id = ?, bucket_key = ? || ':' || bucket_key
                WHERE board_id = ''
                """,
                [default_board_id, default_board_id],
            )

        assignment_columns = {
            str(row[0])
            for row in db.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'rightsizing_plan_assignments'
                """
            ).fetchall()
        }
        if assignment_columns and "board_id" not in assignment_columns:
            # vm_key must stay the raw Azure resource id (compared directly
            # against live inventory elsewhere), so per-board scoping needs
            # a real composite (board_id, vm_key) primary key -- and ALTER
            # cannot safely reshape a primary key identically across DuckDB
            # and PostgreSQL. Migrate by copy-and-rename instead: build the
            # new shape alongside the old table, then swap.
            default_board_id = self._rightsizing_default_board_id(db)
            db.execute(
                """
                CREATE TABLE rightsizing_plan_assignments_v2 (
                    board_id VARCHAR NOT NULL DEFAULT '',
                    vm_key VARCHAR NOT NULL,
                    vm_name VARCHAR NOT NULL DEFAULT '',
                    subscription_name VARCHAR NOT NULL DEFAULT '',
                    bucket_key VARCHAR NOT NULL DEFAULT '__unassigned__',
                    decision VARCHAR NOT NULL DEFAULT 'Pending',
                    note VARCHAR NOT NULL DEFAULT '',
                    ref_monthly_payg DOUBLE,
                    ref_monthly_commitment DOUBLE,
                    ref_monthly_savings DOUBLE,
                    economics_status VARCHAR NOT NULL DEFAULT '',
                    source VARCHAR NOT NULL DEFAULT 'ui',
                    updated_by VARCHAR NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (board_id, vm_key)
                )
                """
            )
            db.execute(
                """
                INSERT INTO rightsizing_plan_assignments_v2 (
                    board_id, vm_key, vm_name, subscription_name,
                    bucket_key, decision, note, source, updated_by,
                    updated_at, ref_monthly_payg,
                    ref_monthly_commitment, ref_monthly_savings,
                    economics_status
                )
                SELECT ?, vm_key, vm_name, subscription_name,
                       CASE WHEN bucket_key IN (
                           '__unassigned__', '__nodata__',
                           '__review__', '__savingsplan__', '__excluded__'
                       ) THEN bucket_key
                       ELSE ? || ':' || bucket_key END,
                       decision, note, source, updated_by, updated_at,
                       ref_monthly_payg, ref_monthly_commitment,
                       ref_monthly_savings, economics_status
                FROM rightsizing_plan_assignments
                """,
                [default_board_id, default_board_id],
            )
            db.execute("DROP TABLE rightsizing_plan_assignments")
            db.execute(
                "ALTER TABLE rightsizing_plan_assignments_v2 "
                "RENAME TO rightsizing_plan_assignments"
            )

        log_columns = {
            str(row[0])
            for row in db.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'rightsizing_plan_log'
                """
            ).fetchall()
        }
        if log_columns and "board_id" not in log_columns:
            default_board_id = self._rightsizing_default_board_id(db)
            db.execute(
                "ALTER TABLE rightsizing_plan_log "
                "ADD COLUMN board_id VARCHAR DEFAULT ''"
            )
            # from_label/to_label are historical bucket_key snapshots, so
            # they need the same board-id prefix the buckets/assignments
            # above just got -- otherwise the decision log can never
            # resolve them back to a human label again (the special
            # pseudo-buckets and a blank "no prior bucket" label are
            # never prefixed).
            db.execute(
                """
                UPDATE rightsizing_plan_log
                SET board_id = ?,
                    from_label = CASE
                        WHEN from_label IN (
                            '__unassigned__', '__nodata__',
                            '__review__', '__savingsplan__', '__excluded__', ''
                        ) THEN from_label
                        ELSE ? || ':' || from_label
                    END,
                    to_label = CASE
                        WHEN to_label IN (
                            '__unassigned__', '__nodata__',
                            '__review__', '__savingsplan__', '__excluded__', ''
                        ) THEN to_label
                        ELSE ? || ':' || to_label
                    END
                WHERE board_id = ''
                """,
                [default_board_id, default_board_id, default_board_id],
            )
