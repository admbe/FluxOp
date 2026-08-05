from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import socket
import threading
import time
from typing import Any, Iterator
from uuid import uuid4

import duckdb
from filelock import FileLock, Timeout as FileLockTimeout

from .confidence import METHOD_VERSION, confidence_score
from .config import settings
from .intelligence import RULE_FRESHNESS_DAYS
from .cost_anomaly import (
    METHOD_VERSION as COST_ANOMALY_METHOD_VERSION,
    evaluate_cost_series,
)
from .forecasting import forecast_daily_cost, forecast_fiscal_year
from .valuation import METHOD_VERSION as VALUATION_METHOD_VERSION, value_opportunity
from .drift import (
    METHOD_VERSION as DRIFT_METHOD_VERSION,
    anomaly_result,
    change_counts,
    classify_changes,
)
from .rightsizing import (
    METHOD_VERSION as RIGHTSIZING_METHOD_VERSION,
    assess_resource,
)
from .pricing import price_profile
from .valuation import monthly_run_rate
from .operational_store import (
    OperationalStore,
    WriterLeaseTimeout as OperationalWriterLeaseTimeout,
)


_DUCKDB_INSERT_BATCH_SIZE = 1000

# A crashed worker's sync claim becomes reclaimable after this long without a
# heartbeat; stage updates extend the claim.
_SYNC_CLAIM_LEASE_SECONDS = float(os.getenv("FLUX_SYNC_CLAIM_LEASE_SECONDS", "900"))

_logger = logging.getLogger("flux.database")

# Lock waits above this many seconds are logged so contention between the web
# process and the synchronization worker is visible in App Service logs.
_LOCK_WAIT_LOG_THRESHOLD_SECONDS = 1.0

# The Cost Management Query API and FOCUS exports name the same service
# differently. These are the PURE RENAMES, verified against production data
# 2026-08-01 (each pair is the same meters under two labels); both ingestion
# paths and a one-time backfill normalize to one canonical label so
# service-level comparisons survive the FOCUS cutover. Deliberately absent:
# FOCUS's semantic regroupings (managed disks booked under Virtual Machines,
# backup protection under Azure Site Recovery, licenses folded into their
# parent service) -- relabeling those would fabricate equivalence between
# genuinely different groupings; cost_report discloses that boundary instead.
_SERVICE_NAME_CANONICAL = {
    "Storage Accounts": "Storage",
    "Azure SQL Database": "SQL Database",
    "Azure NAT Gateway": "NAT Gateway",
    "Azure DB for PostgreSQL": "Azure Database for PostgreSQL",
    "Azure Automation": "Automation",
    "Azure Data Factory v2": "Azure Data Factory",
}


def canonical_service_name(name: str) -> str:
    return _SERVICE_NAME_CANONICAL.get(name, name)


def _canonical_service_name_sql(column: str) -> str:
    """The same mapping as a SQL CASE, for set-based inserts and backfills."""
    branches = " ".join(
        f"WHEN '{source}' THEN '{target}'"
        for source, target in _SERVICE_NAME_CANONICAL.items()
    )
    return f"CASE {column} {branches} ELSE {column} END"


class DatabaseBusyError(RuntimeError):
    """The cross-process DuckDB lease could not be acquired in time.

    Raised only when the owning ``FluxDatabase`` was constructed with a bounded
    ``connect_timeout_seconds``. The API maps this to ``503 Service
    Unavailable`` with a ``Retry-After`` header instead of letting requests
    hang behind a long-running writer.
    """

    def __init__(self, waited_seconds: float):
        self.waited_seconds = waited_seconds
        super().__init__(
            "The analytical database is busy behind a long-running writer; "
            f"gave up after {waited_seconds:.1f}s."
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def json_value(value: Any) -> str:
    return json.dumps(value if value is not None else {}, separators=(",", ":"), default=str)


def parse_iso_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    normalized = str(value).replace("Z", "+00:00")
    if "." in normalized:
        prefix, fraction = normalized.split(".", 1)
        digits = ""
        suffix = ""
        for character in fraction:
            if character.isdigit() and not suffix:
                digits += character
            else:
                suffix += character
        normalized = f"{prefix}.{digits[:6]}{suffix}"
    return datetime.fromisoformat(normalized)


class FluxDatabase:
    def __init__(
        self,
        path: Path,
        default_azure_provider: str = "local_powershell",
        operational_database_url: str = "",
        operational_duckdb_path: Path | None = None,
        focus_cost_enabled: bool = True,
        focus_cost_required: bool = False,
        connect_timeout_seconds: float = -1.0,
    ):
        self.path = Path(path)
        # -1 preserves the historical wait-forever behavior for the worker and
        # command-line jobs; the web process passes a bounded timeout so
        # requests fail fast with 503 instead of hanging behind a writer.
        self.connect_timeout_seconds = connect_timeout_seconds
        self.default_azure_provider = default_azure_provider
        self.focus_cost_enabled = focus_cost_enabled
        self.focus_cost_required = focus_cost_required
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._duckdb_memory_limit = os.getenv(
            "FLUX_DUCKDB_MEMORY_LIMIT",
            "",
        ).strip()
        self._duckdb_temp_directory = Path(
            os.getenv(
                "FLUX_DUCKDB_TEMP_DIRECTORY",
                str(self.path.parent / ".duckdb-tmp"),
            )
        )
        self._duckdb_max_temp_directory_size = os.getenv(
            "FLUX_DUCKDB_MAX_TEMP_DIRECTORY_SIZE",
            "8GB",
        ).strip()
        if self._duckdb_memory_limit:
            self._duckdb_temp_directory.mkdir(parents=True, exist_ok=True)
        self._operational = OperationalStore(
            database_url=operational_database_url,
            duckdb_path=operational_duckdb_path or self.path,
            default_azure_provider=default_azure_provider,
        )
        self._lock = threading.RLock()
        # DuckDB does not support a writer in one process while another process
        # holds a read-only connection to the same file. Every Flux connection
        # therefore participates in one cross-process lease. The historical
        # filename remains stable across deployments.
        self._writer_lock = FileLock(str(self.path) + ".writer.lock")
        # The file lock above only serializes *this* instance: /home is CIFS
        # and flock() there is client-local, so it cannot coordinate the App
        # Service instances that share the mount. The global gate is a
        # PostgreSQL advisory lock (see OperationalStore.duckdb_writer_lease).
        # Depth counter because the advisory lock is taken on a fresh pooled
        # connection each time -- a nested connect() would otherwise block
        # waiting for a lease this same call stack already holds.
        self._writer_lease_depth = 0
        # When an immutable published snapshot is attached, read-only
        # connections open it directly with no cross-process lease and no
        # serialization; the mutable database keeps the lease for writers.
        self._read_snapshot_path: Path | None = None

    def _ensure_database_file(self) -> None:
        """Bootstrap a missing database before DuckDB opens the persistent path.

        App Service storage can briefly report a persisted file as missing while
        the /home mount settles after deployment. DuckDB normally creates a
        missing database itself, but some Linux/Azure Files combinations abort
        in native code while obtaining file statistics. Creating a valid
        database at a temporary path and moving it into place avoids that native
        failure. The caller holds the cross-process writer lease, and an
        existing database is never replaced.
        """
        if self.path.exists():
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.bootstrap-{os.getpid()}-{uuid4().hex}"
        )
        try:
            bootstrap = duckdb.connect(str(temporary))
            bootstrap.close()
            if not self.path.exists():
                temporary.rename(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _global_writer_lease(self, timeout: float) -> Iterator[None]:
        """Cross-instance half of the lease, reentrant within one call stack.

        Always taken *after* the local file lock so every caller acquires the
        two in the same order and they cannot deadlock against each other.
        """
        if self._writer_lease_depth > 0:
            self._writer_lease_depth += 1
            try:
                yield
            finally:
                self._writer_lease_depth -= 1
            return
        with self._operational.duckdb_writer_lease(timeout=timeout):
            self._writer_lease_depth += 1
            try:
                yield
            finally:
                self._writer_lease_depth -= 1

    @contextmanager
    def writer_lease(self, timeout: float = 0) -> Iterator[None]:
        """Hold the one lease every DuckDB writer must own.

        Two gates: the file lock serializes this instance's threads and
        processes, the advisory lock serializes the instances themselves.
        """
        with self._writer_lock.acquire(timeout=timeout):
            with self._global_writer_lease(timeout):
                yield

    @property
    def operational_backend(self) -> str:
        return self._operational.backend

    @contextmanager
    def operational_connect(self, read_only: bool = False) -> Iterator[Any]:
        with self._operational.connect(read_only=read_only) as db:
            yield db

    @contextmanager
    def singleton_lease(self, name: str) -> Iterator[None]:
        """Cross-process singleton lease through the operational store."""
        with self._operational.singleton_lease(name):
            yield

    def claim_throttle_slot(self, name: str, interval_seconds: float) -> float:
        """Shared request-rate slot through the operational store."""
        return self._operational.claim_throttle_slot(name, interval_seconds)

    def register_throttle_cooldown(self, name: str, cooldown_seconds: float) -> None:
        """Shared upstream-throttle cooldown through the operational store."""
        self._operational.register_throttle_cooldown(name, cooldown_seconds)

    def claim_cost_management_quota(
        self,
        name: str,
        estimated_qpu: float,
        windows: list[tuple[int, float]],
        *,
        minimum_interval_seconds: float = 0.0,
    ) -> tuple[float, str | None]:
        """Reserve durable tenant-wide Cost Management quota capacity."""
        return self._operational.claim_cost_management_quota(
            name,
            estimated_qpu,
            windows,
            minimum_interval_seconds=minimum_interval_seconds,
        )

    def reconcile_cost_management_quota(
        self,
        name: str,
        reservation_id: str | None,
        *,
        consumed_qpu: float | None = None,
    ) -> None:
        """Record Azure's actual QPU consumption for a reservation."""
        self._operational.reconcile_cost_management_quota(
            name,
            reservation_id,
            consumed_qpu=consumed_qpu,
        )

    def register_cost_management_quota_cooldown(
        self, name: str, cooldown_seconds: float
    ) -> None:
        """Persist a server-directed tenant quota cooldown."""
        self._operational.register_cost_management_quota_cooldown(
            name, cooldown_seconds
        )

    @staticmethod
    def stale_triggered_job_locks(
        now: datetime,
        stale_after_seconds: float = 900,
    ) -> list[tuple[str, float]]:
        """Triggered WebJobs whose Kudu lock outlived its owner.

        Kudu writes triggeredJob.lock (+ .heartbeat) while a triggered job
        runs and removes both on clean completion. A process killed
        mid-run -- a deploy restart, typically -- leaves them behind, and
        Kudu then reports the job as Running forever, silently skipping
        every scheduled run (third orphaned-lock variant this week,
        2026-08-01: flux-cost-history missed its 12:30 schedule for two
        days with no error anywhere). A present lock with a heartbeat
        older than stale_after_seconds is that state; a fresh heartbeat is
        just a job legitimately running.
        """
        root = Path(
            os.environ.get("FLUX_WEBJOBS_DATA_ROOT", "/home/data/jobs")
        ) / "triggered"
        results: list[tuple[str, float]] = []
        try:
            job_dirs = [item for item in root.iterdir() if item.is_dir()]
        except OSError:
            return results
        for job_dir in job_dirs:
            lock = job_dir / "triggeredJob.lock"
            heartbeat = job_dir / "triggeredJob.lock.heartbeat"
            probe = heartbeat if heartbeat.exists() else lock
            try:
                mtime = datetime.fromtimestamp(
                    probe.stat().st_mtime, tz=timezone.utc
                )
            except OSError:
                continue
            age = (now - mtime).total_seconds()
            if age > stale_after_seconds:
                results.append((job_dir.name, age))
        return results

    def prune_analytics_history(
        self,
        *,
        history_days: int = 60,
        telemetry_sample_days: int = 45,
    ) -> dict[str, int]:
        """Bound the append-only history tables that dominate snapshot size.

        Production measurement 2026-08-02: the 4.7 GiB snapshot was ~91%
        live data, dominated by unbounded per-run histories (3.3M raw
        telemetry samples in ~2.5 weeks, 400K+ opportunity scoring rows) --
        not dead blocks, so compaction cannot help; retention can.

        The invariant is that pruning NEVER changes what any *_current
        table or view resolves to:
        - source_sync_state-driven tables keep every row of the latest
          snapshot per (source, scope), so a stalled source keeps serving
          its last-good data no matter how old.
        - Ranked tables (confidence/valuation) only drop rows that a newer
          row for the same (resource, opportunity) already supersedes.
        - Rightsizing keeps the newest run in full.
        Aging survives pruning: the newest confidence row carries
        first_seen/last_seen forward, so dropping superseded rows does not
        reset how long an opportunity has persisted.
        """
        history_cutoff = utc_now() - timedelta(days=max(1, history_days))
        telemetry_cutoff = utc_now() - timedelta(
            days=max(1, telemetry_sample_days)
        )
        latest_per_scope = (
            "SELECT arg_max(snapshot_id, observed_at) "
            "FROM source_sync_state GROUP BY source, scope_id"
        )
        observed_tables = (
            "resource_snapshots",
            "cost_snapshots",
            "commitment_cost_snapshots",
            "advisor_recommendation_snapshots",
            "rule_opportunity_snapshots",
            "policy_posture_snapshots",
            "policy_resource_snapshots",
        )
        ranked_tables = (
            "opportunity_confidence_snapshots",
            "opportunity_valuation_snapshots_v2",
        )
        deleted: dict[str, int] = {}
        with self.connect() as db:
            for table in observed_tables:
                deleted[table] = int(
                    db.execute(
                        f"""
                        DELETE FROM {table}
                        WHERE observed_at < ?
                          AND snapshot_id NOT IN ({latest_per_scope})
                        """,
                        [history_cutoff],
                    ).fetchone()[0]
                )
            for table in ranked_tables:
                deleted[table] = int(
                    db.execute(
                        f"""
                        DELETE FROM {table} AS old
                        WHERE old.computed_at < ?
                          AND EXISTS (
                              SELECT 1 FROM {table} AS newer
                              WHERE newer.resource_id = old.resource_id
                                AND newer.opportunity_type =
                                    old.opportunity_type
                                AND (
                                    newer.computed_at > old.computed_at
                                    OR (
                                        newer.computed_at = old.computed_at
                                        AND newer.snapshot_id > old.snapshot_id
                                    )
                                )
                          )
                        """,
                        [history_cutoff],
                    ).fetchone()[0]
                )
            deleted["rightsizing_recommendation_snapshots"] = int(
                db.execute(
                    """
                    DELETE FROM rightsizing_recommendation_snapshots
                    WHERE computed_at < ?
                      AND run_id <> (
                          SELECT arg_max(run_id, computed_at)
                          FROM rightsizing_recommendation_snapshots
                      )
                    """,
                    [history_cutoff],
                ).fetchone()[0]
            )
            deleted["telemetry_metric_samples"] = int(
                db.execute(
                    "DELETE FROM telemetry_metric_samples "
                    "WHERE observed_at < ?",
                    [telemetry_cutoff],
                ).fetchone()[0]
            )
        return deleted

    def pipeline_status(self) -> dict[str, Any]:
        """One-call end-to-end view of the data pipeline's moving parts.

        Reads only the operational control plane (never DuckDB), so it stays
        fast and answers even while analytical work is heavy: sync queue and
        claim ages, publication currency, staged-apply backlog, and shared
        throttle state.
        """
        now = utc_now()

        def age_seconds(value: Any) -> float | None:
            moment = parse_iso_timestamp(value) if not isinstance(value, datetime) else value
            if moment is None:
                return None
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return round((now - moment).total_seconds(), 1)

        def iso(value: Any) -> str | None:
            moment = parse_iso_timestamp(value) if not isinstance(value, datetime) else value
            return moment.isoformat() if moment else None

        with self.operational_connect(read_only=True) as db:
            sync_rows = db.execute(
                """
                SELECT id, trigger, status, stage, stage_message, started_at,
                       claimed_by, claimed_at, claim_expires_at
                FROM sync_runs
                WHERE status IN ('queued', 'running')
                ORDER BY started_at ASC
                """
            ).fetchall()
            last_completed = db.execute(
                """
                SELECT id, status, completed_at, message
                FROM sync_runs
                WHERE status IN ('succeeded', 'failed')
                ORDER BY completed_at DESC
                LIMIT 1
                """
            ).fetchone()
            publication = db.execute(
                """
                SELECT version, status, file_name, file_size_bytes, generated_at
                FROM analytics_publications
                WHERE status = 'approved'
                ORDER BY version DESC
                LIMIT 1
                """
            ).fetchone()
            rejected = db.execute(
                """
                SELECT count(*) FROM analytics_publications
                WHERE status = 'rejected'
                """
            ).fetchone()
            apply_summary = db.execute(
                """
                SELECT status, count(*), min(created_at)
                FROM analytics_apply_jobs
                GROUP BY status
                """
            ).fetchall()
            throttles = db.execute(
                "SELECT name, next_allowed_at FROM throttle_state ORDER BY name"
            ).fetchall()

        syncs = [
            {
                "id": str(row[0]),
                "trigger": str(row[1] or "manual"),
                "status": str(row[2]),
                "stage": str(row[3] or ""),
                "stageMessage": str(row[4] or ""),
                "queuedAgeSeconds": age_seconds(row[5]),
                "claimedBy": row[6],
                "claimAgeSeconds": age_seconds(row[7]),
                "claimExpiresAt": iso(row[8]),
            }
            for row in sync_rows
        ]
        # Work sitting unclaimed is a distinct failure from work running too
        # long: it means no worker is consuming the queue at all. A stale Kudu
        # singleton lock produced exactly this for hours, silently, because
        # every collector still reported success while nothing drained.
        warnings: list[str] = []
        unclaimed = [
            item for item in syncs
            if item["status"] == "queued" and (item["queuedAgeSeconds"] or 0) > 900
        ]
        if unclaimed:
            oldest = max(item["queuedAgeSeconds"] or 0 for item in unclaimed)
            warnings.append(
                f"{len(unclaimed)} synchronization(s) queued and unclaimed for "
                f"up to {oldest / 60:.0f} minutes: no worker is consuming the "
                "queue. Check the sync worker is running and holds its "
                "singleton lock."
            )
        expired = [
            item for item in syncs
            if item["status"] == "running"
            and item["claimExpiresAt"]
            and item["claimExpiresAt"] < now.isoformat()
        ]
        if expired:
            warnings.append(
                f"{len(expired)} running synchronization(s) hold an expired "
                "claim; a replacement worker should reclaim them."
            )
        applies: dict[str, Any] = {"staged": 0, "failed": 0, "applied": 0}
        oldest_staged = None
        for status, count, oldest in apply_summary:
            applies[str(status)] = int(count)
            if str(status) == "staged":
                oldest_staged = age_seconds(oldest)
        if applies.get("failed"):
            warnings.append(
                f"{applies['failed']} analytics apply job(s) failed after "
                "exhausting retries."
            )
        publication_age = age_seconds(publication[4]) if publication else None
        if publication_age is not None and publication_age > 86_400:
            warnings.append(
                f"The newest approved analytical snapshot is "
                f"{publication_age / 3600:.0f} hours old."
            )
        for job_name, lock_age in self.stale_triggered_job_locks(now):
            warnings.append(
                f"Triggered WebJob '{job_name}' holds a lock whose heartbeat "
                f"is {lock_age / 3600:.1f} hours stale: the run died without "
                "releasing it and Kudu is silently skipping every scheduled "
                "run. Delete triggeredJob.lock and "
                "triggeredJob.lock.heartbeat under "
                f"/home/data/jobs/triggered/{job_name}/ and re-trigger."
            )
        return {
            "generatedAt": now.isoformat(),
            "warnings": warnings,
            "syncs": {
                "pending": syncs,
                "queuedCount": sum(1 for s in syncs if s["status"] == "queued"),
                "runningCount": sum(1 for s in syncs if s["status"] == "running"),
                "lastCompleted": (
                    {
                        "id": str(last_completed[0]),
                        "status": str(last_completed[1]),
                        "completedAgeSeconds": age_seconds(last_completed[2]),
                        "message": str(last_completed[3] or "")[:200],
                    }
                    if last_completed
                    else None
                ),
            },
            "publication": (
                {
                    "version": int(publication[0]),
                    "fileName": str(publication[2]),
                    "fileSizeBytes": int(publication[3]),
                    "ageSeconds": age_seconds(publication[4]),
                    "rejectedCount": int(rejected[0]) if rejected else 0,
                }
                if publication
                else {"version": None, "rejectedCount": int(rejected[0]) if rejected else 0}
            ),
            "applyJobs": {
                "stagedCount": applies.get("staged", 0),
                "failedCount": applies.get("failed", 0),
                "appliedCount": applies.get("applied", 0),
                "oldestStagedAgeSeconds": oldest_staged,
            },
            "throttles": [
                {
                    "name": str(name),
                    "nextAllowedAt": iso(next_allowed),
                    "blockedForSeconds": (
                        max(0.0, -(age_seconds(next_allowed) or 0.0))
                    ),
                }
                for name, next_allowed in throttles
            ],
        }

    @contextmanager
    def _optional_operational_connect(
        self,
        connection: Any | None = None,
        read_only: bool = True,
    ) -> Iterator[Any]:
        """Reuse an open operational connection, or open a pooled one.

        Several operational read paths (``operational_health`` and friends)
        fan out into multiple helper methods that each opened their own
        PostgreSQL connection. Threading one connection through them removes
        the repeated pool checkout round-trips and keeps a single snapshot.
        """
        if connection is not None:
            yield connection
            return
        with self.operational_connect(read_only=read_only) as db:
            yield db

    def next_analytics_publication_version(self) -> int:
        with self.operational_connect(read_only=True) as db:
            row = db.execute(
                "SELECT coalesce(max(version), 0) FROM analytics_publications"
            ).fetchone()
        return int(row[0] if row else 0) + 1

    def record_analytics_publication(
        self,
        status: str,
        file_name: str,
        checksum: str,
        file_size_bytes: int,
        row_counts: dict[str, int],
        message: str = "",
        version: int | None = None,
        duration_ms: int = 0,
    ) -> dict[str, Any]:
        if version is None:
            version = self.next_analytics_publication_version()
        generated_at = utc_now()
        with self.operational_connect() as db:
            db.execute(
                """
                INSERT INTO analytics_publications (
                    version, status, file_name, checksum_sha256,
                    file_size_bytes, row_counts, message, generated_at,
                    duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    version,
                    status,
                    file_name,
                    checksum,
                    file_size_bytes,
                    json_value(row_counts),
                    message,
                    generated_at,
                    int(duration_ms),
                ],
            )
            db.commit()
        return {
            "version": version,
            "status": status,
            "fileName": file_name,
            "checksum": checksum,
            "fileSizeBytes": file_size_bytes,
            "rowCounts": row_counts,
            "message": message,
            "generatedAt": generated_at.isoformat(),
            "durationMs": int(duration_ms),
        }

    def latest_analytics_publication(self) -> dict[str, Any] | None:
        with self.operational_connect(read_only=True) as db:
            row = db.execute(
                """
                SELECT version, status, file_name, checksum_sha256,
                       file_size_bytes, row_counts, message, generated_at
                FROM analytics_publications
                WHERE status = 'approved'
                ORDER BY version DESC
                LIMIT 1
                """
            ).fetchone()
        if not row:
            return None
        return {
            "version": int(row[0]),
            "status": str(row[1]),
            "fileName": str(row[2]),
            "checksum": str(row[3]),
            "fileSizeBytes": int(row[4]),
            "rowCounts": json.loads(str(row[5]) or "{}"),
            "message": str(row[6]),
            "generatedAt": str(row[7]),
        }

    def prune_analytics_publications(
        self,
        keep: int,
        daily_retention_days: int = 0,
    ) -> list[str]:
        """Delete approved publications beyond the retention policy.

        Retains the newest ``keep`` versions plus the newest version of each
        UTC day for ``daily_retention_days`` days — approved snapshots double
        as the analytical backup tier, so daily retention replaces the legacy
        full-database backup copies. Non-approved audit rows are always kept.
        Returns the file names whose storage objects should be deleted.
        """
        now = utc_now()
        with self.operational_connect() as db:
            rows = db.execute(
                """
                SELECT version, file_name, generated_at
                FROM analytics_publications
                WHERE status = 'approved'
                ORDER BY version DESC
                """
            ).fetchall()
            retained = {int(row[0]) for row in rows[: max(1, keep)]}
            if daily_retention_days > 0:
                newest_per_day: dict[str, int] = {}
                for version, _, generated_at in rows:
                    moment = (
                        generated_at
                        if isinstance(generated_at, datetime)
                        else parse_iso_timestamp(generated_at)
                    )
                    if moment is None:
                        continue
                    if moment.tzinfo is None:
                        moment = moment.replace(tzinfo=timezone.utc)
                    if (now - moment).days >= daily_retention_days:
                        continue
                    day = moment.date().isoformat()
                    if int(version) > newest_per_day.get(day, -1):
                        newest_per_day[day] = int(version)
                retained.update(newest_per_day.values())
            stale = [
                (int(version), str(name))
                for version, name, _ in rows
                if int(version) not in retained
            ]
            for version, _ in stale:
                db.execute(
                    "DELETE FROM analytics_publications WHERE version = ?",
                    [version],
                )
            db.commit()
        return [name for _, name in stale]

    def attach_read_snapshot(self, path: Path | None) -> None:
        """Route subsequent read-only connections to an immutable snapshot.

        In-flight connections are unaffected; they hold their own handle to
        the previously attached file. Passing ``None`` restores direct
        lease-guarded reads of the mutable database.
        """
        self._read_snapshot_path = Path(path) if path else None

    @property
    def read_snapshot_path(self) -> Path | None:
        return self._read_snapshot_path

    @contextmanager
    def connect(self, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
        snapshot = self._read_snapshot_path
        if read_only and snapshot is not None:
            # Immutable published snapshot: no cross-process lease, no
            # in-process serialization, parallel readers are safe.
            yield from self._open_connection(read_only=True, path=snapshot)
            return
        waited_from = time.monotonic()
        try:
            lease = self._writer_lock.acquire(timeout=self.connect_timeout_seconds)
        except FileLockTimeout:
            waited = time.monotonic() - waited_from
            _logger.error(
                "DuckDB lease timeout after %.1fs (read_only=%s); "
                "another process is holding the writer lease.",
                waited,
                read_only,
            )
            raise DatabaseBusyError(waited) from None
        with lease:
            # Second gate: the file lock above is client-local on CIFS, so it
            # does not exclude the other App Service instance. Without this
            # advisory lock two instances write the same file concurrently.
            remaining = (
                -1.0
                if self.connect_timeout_seconds < 0
                else max(0.0, self.connect_timeout_seconds - (time.monotonic() - waited_from))
            )
            try:
                global_lease = self._global_writer_lease(remaining)
                global_lease.__enter__()
            except OperationalWriterLeaseTimeout:
                waited = time.monotonic() - waited_from
                _logger.error(
                    "DuckDB global lease timeout after %.1fs (read_only=%s); "
                    "another instance is holding the writer lease.",
                    waited,
                    read_only,
                )
                raise DatabaseBusyError(waited) from None
            try:
                waited = time.monotonic() - waited_from
                if waited > _LOCK_WAIT_LOG_THRESHOLD_SECONDS:
                    _logger.warning(
                        "DuckDB lease acquired after waiting %.1fs (read_only=%s).",
                        waited,
                        read_only,
                    )
                with self._lock:
                    yield from self._open_connection(read_only=read_only)
            finally:
                global_lease.__exit__(None, None, None)

    def _open_connection(
        self, read_only: bool = False, path: Path | None = None
    ) -> Iterator[duckdb.DuckDBPyConnection]:
        target = path or self.path
        if not read_only:
            self._ensure_database_file()
        connection = None
        for attempt in range(6):
            try:
                connection = duckdb.connect(
                    str(target),
                    read_only=read_only,
                )
                connection.execute("SET threads = 1")
                connection.execute("SET preserve_insertion_order = false")
                if self._duckdb_memory_limit:
                    connection.execute(
                        "SET memory_limit = ?",
                        [self._duckdb_memory_limit],
                    )
                    connection.execute(
                        "SET temp_directory = ?",
                        [str(self._duckdb_temp_directory)],
                    )
                    connection.execute(
                        "SET max_temp_directory_size = ?",
                        [self._duckdb_max_temp_directory_size],
                    )
                break
            except duckdb.IOException as error:
                message = str(error).lower()
                transient_lock = (
                    "could not set lock" in message
                    or "conflicting lock" in message
                )
                if not transient_lock or attempt == 5:
                    raise
                time.sleep(min(0.25 * (2**attempt), 2.0))
        if connection is None:
            raise RuntimeError("DuckDB connection could not be opened.")
        try:
            yield connection
        finally:
            connection.close()

    def init_operational(self) -> None:
        """Initialize only the operational control plane."""
        self._operational.init()

    _MATERIALIZED_TABLES = {
        "resources_current": """
            WITH latest AS (
                SELECT arg_max(snapshot_id, observed_at) AS snapshot_id
                FROM source_sync_state
                WHERE source = 'AzureResourceGraph'
                  AND scope_id = 'configured-subscriptions'
            )
            SELECT resource.*
            FROM resource_snapshots AS resource
            JOIN latest ON latest.snapshot_id = resource.snapshot_id
        """,
        "costs_current": """
            WITH latest AS (
                SELECT
                    source,
                    scope_id,
                    arg_max(snapshot_id, observed_at) AS snapshot_id
                FROM source_sync_state
                WHERE source IN ('ActualCost', 'AmortizedCost')
                GROUP BY source, scope_id
            )
            SELECT cost.*
            FROM cost_snapshots AS cost
            JOIN latest
              ON latest.snapshot_id = cost.snapshot_id
             AND latest.source = cost.cost_type
             AND latest.scope_id = cost.subscription_id
        """,
        "commitment_costs_current": """
            WITH latest AS (
                SELECT
                    scope_id,
                    arg_max(snapshot_id, observed_at) AS snapshot_id
                FROM source_sync_state
                WHERE source = 'CommitmentCoverage'
                GROUP BY scope_id
            )
            SELECT cost.*
            FROM commitment_cost_snapshots AS cost
            JOIN latest
              ON latest.snapshot_id = cost.snapshot_id
             AND latest.scope_id = cost.subscription_id
        """,
        "policy_posture_current": """
            WITH latest AS (
                SELECT arg_max(snapshot_id, observed_at) AS snapshot_id
                FROM source_sync_state
                WHERE source = 'AzurePolicy'
                  AND scope_id = 'configured-subscriptions'
            )
            SELECT posture.*
            FROM policy_posture_snapshots AS posture
            JOIN latest ON latest.snapshot_id = posture.snapshot_id
        """,
        "advisor_recommendations_current": """
            WITH latest AS (
            SELECT arg_max(snapshot_id, observed_at) AS snapshot_id
            FROM source_sync_state
            WHERE source = 'AzureAdvisor'
            AND scope_id = 'configured-subscriptions'
            ), ranked AS (
            SELECT recommendation.*,
            row_number() OVER (
            PARTITION BY recommendation.recommendation_id
            ORDER BY recommendation.observed_at DESC
            ) AS recommendation_rank
            FROM advisor_recommendation_snapshots AS recommendation
            JOIN latest
            ON latest.snapshot_id = recommendation.snapshot_id
            )
            SELECT * EXCLUDE (recommendation_rank)
            FROM ranked
            WHERE recommendation_rank = 1
        """,
        "rule_opportunities_current": """
            WITH latest AS (
            SELECT arg_max(snapshot_id, observed_at) AS snapshot_id
            FROM source_sync_state
            WHERE source = 'FluxIntelligence'
            AND scope_id = 'configured-subscriptions'
            ), ranked AS (
            SELECT finding.*,
            row_number() OVER (
            PARTITION BY finding.finding_id
            ORDER BY finding.observed_at DESC
            ) AS finding_rank
            FROM rule_opportunity_snapshots AS finding
            JOIN latest ON latest.snapshot_id = finding.snapshot_id
            )
            SELECT * EXCLUDE (finding_rank)
            FROM ranked
            WHERE finding_rank = 1
        """,
        "opportunity_confidence_current": """
            WITH ranked AS (
            SELECT score.*,
            row_number() OVER (
            PARTITION BY resource_id, opportunity_type
            ORDER BY computed_at DESC, snapshot_id DESC
            ) AS rank
            FROM opportunity_confidence_snapshots AS score
            )
            SELECT * EXCLUDE (rank) FROM ranked WHERE rank = 1
        """,
        "opportunity_valuation_current": """
            WITH ranked AS (
            SELECT valuation.*,
            row_number() OVER (
            PARTITION BY resource_id, opportunity_type
            ORDER BY computed_at DESC, snapshot_id DESC
            ) AS rank
            FROM opportunity_valuation_snapshots_v2 AS valuation
            )
            SELECT * EXCLUDE (rank) FROM ranked WHERE rank = 1
        """,
    }

    def _refresh_materialized_tables_internal(self, db: Any) -> None:
        """Rebuild the hot materialized tables in-place.

        Uses CREATE OR REPLACE TABLE so DuckDB atomically swaps the new
        result set. Called from init() and refresh_current_views().

        Each of these was previously a view that recomputed a window
        function over the full snapshot history on every read, which made
        the opportunity, advisor, and right-sizing paths cost seconds per
        request. Databases created before the change still hold a view of
        the same name, so drop it first.
        """
        existing_views = {
            str(row[0])
            for row in db.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_type = 'VIEW'
                """
            ).fetchall()
        }
        for name, query in self._MATERIALIZED_TABLES.items():
            if name in existing_views:
                db.execute(f"DROP VIEW {name}")
            db.execute(f"CREATE OR REPLACE TABLE {name} AS {query}")
        # The semantic layer's governed views sit over the tables rebuilt
        # above; recreating them here keeps every definition current in the
        # mutable database (snapshot candidates get their own pass).
        from .semantic_layer import create_semantic_views

        create_semantic_views(db)

    def refresh_current_views(self) -> None:
        """Rebuild the four materialized current-snapshot tables.

        Call after store_snapshot, store_policy_posture, or any write that
        changes source_sync_state. Until this is called, the materialized
        tables reflect the previous snapshot; reads remain correct but stale.
        """
        with self.connect() as db:
            self._refresh_materialized_tables_internal(db)

    def store_commitments(
        self,
        snapshot_id: str,
        reservations: list[dict[str, Any]],
        recommendations: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Persist one commitments collection run.

        Rows land under a shared snapshot id and the *_current views follow
        the newest snapshot, so a partial run (for example, inventory denied
        but recommendations returned) never erases the previous good data of
        the other feed: empty lists are simply not inserted.
        """
        now = utc_now()
        with self.writer_lease(), self.connect() as db:
            for reservation in reservations:
                db.execute(
                    """
                    INSERT INTO reservation_snapshots VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        snapshot_id,
                        now,
                        reservation.get("reservationId") or "",
                        reservation.get("orderId") or "",
                        reservation.get("displayName") or "",
                        reservation.get("sku") or "",
                        reservation.get("resourceType") or "",
                        reservation.get("region") or "",
                        int(reservation.get("quantity") or 0),
                        reservation.get("term") or "",
                        reservation.get("scopeType") or "",
                        reservation.get("state") or "",
                        reservation.get("expiryDate"),
                        reservation.get("utilization1d"),
                        reservation.get("utilization7d"),
                        reservation.get("utilization30d"),
                    ],
                )
            for recommendation in recommendations:
                db.execute(
                    """
                    INSERT INTO reservation_recommendation_snapshots
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        snapshot_id,
                        now,
                        recommendation.get("subscriptionId") or "",
                        recommendation.get("subscriptionName") or "",
                        recommendation.get("scope") or "",
                        recommendation.get("resourceType") or "",
                        recommendation.get("sku") or "",
                        recommendation.get("region") or "",
                        recommendation.get("term") or "",
                        recommendation.get("lookBack") or "",
                        float(recommendation.get("recommendedQuantity") or 0),
                        recommendation.get("costWithoutCommitment"),
                        recommendation.get("costWithCommitment"),
                        recommendation.get("netSavings"),
                    ],
                )
            # Register on the freshness board. Without this the commitments
            # feed could go stale forever with no stale/degraded signal
            # anywhere (scheduling assessment finding, 2026-08-01). A partial
            # run (one feed empty) still counts as an observation: the run
            # happened; emptiness is visible in row_count.
            db.execute(
                "INSERT INTO source_sync_state VALUES (?, ?, ?, ?, ?)",
                [
                    snapshot_id,
                    now,
                    "Commitments",
                    "configured-subscriptions",
                    len(reservations) + len(recommendations),
                ],
            )
        return {
            "reservations": len(reservations),
            "recommendations": len(recommendations),
        }

    def store_price_sheet(self, csv_files: list[Any]) -> int:
        """Replace the negotiated price sheet with the newest export run.

        The export's column names differ between EA and MCA accounts, so
        the stage is read with every column as text and normalized by
        whichever variant is present; missing economics stay NULL rather
        than inventing zeros.
        """
        paths = [str(path) for path in csv_files]
        if not paths:
            return 0
        with self.writer_lease(), self.connect() as db:
            db.execute(
                """
                CREATE OR REPLACE TEMP TABLE price_sheet_stage AS
                SELECT * FROM read_csv(
                    ?, header = true, all_varchar = true,
                    union_by_name = true, auto_detect = true
                )
                """,
                [paths],
            )
            present = {
                str(row[0]).lower(): str(row[0])
                for row in db.execute(
                    "DESCRIBE price_sheet_stage"
                ).fetchall()
            }

            def text(*candidates: str) -> str:
                for candidate in candidates:
                    if candidate.lower() in present:
                        return f'coalesce("{present[candidate.lower()]}", \'\')'
                return "''"

            def number(*candidates: str) -> str:
                for candidate in candidates:
                    if candidate.lower() in present:
                        return (
                            f'try_cast("{present[candidate.lower()]}"'
                            " AS DOUBLE)"
                        )
                return "CAST(NULL AS DOUBLE)"

            db.execute(
                f"""
                CREATE OR REPLACE TABLE price_sheet_current AS
                SELECT
                    {text("meterId", "meterID")} AS meter_id,
                    {text("meterName")} AS meter_name,
                    {text("meterCategory", "serviceFamily")}
                        AS service_family,
                    {text("product", "productName")} AS product,
                    {text("skuId", "partNumber", "productOrderId")}
                        AS sku_id,
                    {text("unitOfMeasure")} AS unit_of_measure,
                    {text("priceType")} AS price_type,
                    {number("unitPrice")} AS unit_price,
                    {number("basePrice")} AS base_price,
                    {number("marketPrice")} AS market_price,
                    {text("currency", "currencyCode")} AS currency
                FROM price_sheet_stage
                """
            )
            count = db.execute(
                "SELECT COUNT(*) FROM price_sheet_current"
            ).fetchone()
            # Register on the freshness board (scheduling assessment finding,
            # 2026-08-01: the price sheet could go stale forever unnoticed).
            db.execute(
                "INSERT INTO source_sync_state VALUES (?, ?, ?, ?, ?)",
                [
                    f"price-sheet-{utc_now().strftime('%Y%m%dT%H%M%S')}",
                    utc_now(),
                    "PriceSheet",
                    "billing-account",
                    int(count[0]) if count else 0,
                ],
            )
        return int(count[0]) if count else 0

    def semantic_catalog(self) -> dict[str, Any]:
        """The semantic registry annotated with per-model availability.

        A model is available when its view exists in the analytics store
        this process is currently serving (published snapshot in
        production), so the explorer can hide models a given snapshot
        predates.
        """
        from .semantic_layer import find_model, semantic_catalog

        catalog = semantic_catalog()
        with self.connect(read_only=True) as db:
            rows = db.execute(
                "SELECT table_name, column_name FROM information_schema.columns"
                " WHERE table_name LIKE 'semantic_%'"
            ).fetchall()
        columns: dict[str, set[str]] = {}
        for table, column in rows:
            columns.setdefault(str(table), set()).add(str(column))

        for model in catalog["models"]:
            view = f"semantic_{model['name']}"
            available = columns.get(view)
            model["available"] = available is not None
            if not available:
                continue
            # Code deploys ahead of data: a dimension added to the registry
            # only exists once the next snapshot rebuilds the view. Offering
            # it before then produced an unreadable binder error, so hide
            # what the served snapshot cannot answer.
            registry = find_model(model["name"])
            if registry is None:
                continue
            model["dimensions"] = [
                entry for entry in model["dimensions"]
                if (
                    registry.dimension(entry["name"]) is not None
                    and registry.dimension(entry["name"]).column in available
                )
            ]
        return catalog

    def run_semantic_query(self, query: Any) -> dict[str, Any]:
        """Execute a governed semantic query against the analytics store."""
        from datetime import date as _date, datetime as _datetime
        from decimal import Decimal

        from .semantic_layer import (
            SemanticQueryError,
            build_semantic_query,
            find_model,
        )

        sql, parameters, columns, applied_defaults = build_semantic_query(query)
        with self.connect(read_only=True) as db:
            model = find_model(query.model)
            present = db.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_name = ?",
                [model.view_name if model else ""],
            ).fetchone()
            if not (present and present[0]):
                # The serving snapshot predates the semantic layer; the next
                # publication after this deploy carries the views.
                raise SemanticQueryError(
                    f"Model {query.model!r} is not available in the current "
                    "analytics snapshot yet. It becomes available after the "
                    "next data synchronization publishes."
                )
            if model is not None and query.dimensions:
                view_columns = {
                    str(row[0])
                    for row in db.execute(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name = ?",
                        [model.view_name],
                    ).fetchall()
                }
                for name in query.dimensions:
                    dimension = model.dimension(name)
                    if dimension and dimension.column not in view_columns:
                        # Registry is ahead of the published snapshot. Say so
                        # plainly; DuckDB's own error for this is an opaque
                        # complaint about GROUP BY aliases.
                        raise SemanticQueryError(
                            f"Dimension {name!r} is defined but not present "
                            "in the analytics snapshot currently being "
                            "served. It becomes available after the next "
                            "data synchronization publishes."
                        )
            raw = db.execute(sql, parameters).fetchall()

        def cell(value: Any) -> Any:
            if isinstance(value, (_datetime, _date)):
                return value.isoformat()
            if isinstance(value, Decimal):
                return float(value)
            return value

        return {
            "columns": columns,
            "rows": [[cell(value) for value in row] for row in raw],
            "sql": sql,
            "rowCount": len(raw),
            "appliedDefaults": applied_defaults,
        }

    def init(self) -> None:
        self.init_operational()
        with self.connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS azure_integration (
                    id VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    tenant_id VARCHAR,
                    enabled BOOLEAN NOT NULL,
                    auth_mode VARCHAR NOT NULL,
                    subscriptions_json JSON NOT NULL,
                    last_sync_at TIMESTAMPTZ,
                    last_sync_status VARCHAR NOT NULL,
                    last_sync_message VARCHAR NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL
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
                    estimated_cost_usd DOUBLE NOT NULL DEFAULT 0,
                    tool_names_json JSON NOT NULL DEFAULT '[]',
                    tool_call_count INTEGER NOT NULL DEFAULT 0,
                    error_code VARCHAR NOT NULL DEFAULT '',
                    feedback_rating VARCHAR NOT NULL DEFAULT '',
                    feedback_reason VARCHAR NOT NULL DEFAULT '',
                    feedback_at TIMESTAMPTZ
                );
                ALTER TABLE intelligence_usage_events ADD COLUMN IF NOT EXISTS
                    model_latency_ms INTEGER DEFAULT 0;
                ALTER TABLE intelligence_usage_events ADD COLUMN IF NOT EXISTS
                    governed_tool_latency_ms INTEGER DEFAULT 0;
                ALTER TABLE intelligence_usage_events ADD COLUMN IF NOT EXISTS
                    database_latency_ms INTEGER DEFAULT 0;
                ALTER TABLE intelligence_usage_events ADD COLUMN IF NOT EXISTS
                    validation_latency_ms INTEGER DEFAULT 0;
                ALTER TABLE intelligence_usage_events ADD COLUMN IF NOT EXISTS
                    application_latency_ms INTEGER DEFAULT 0;
                ALTER TABLE intelligence_usage_events ADD COLUMN IF NOT EXISTS
                    model_call_count INTEGER DEFAULT 0;
                ALTER TABLE intelligence_usage_events ADD COLUMN IF NOT EXISTS
                    tool_latency_json JSON DEFAULT '[]';
                ALTER TABLE intelligence_usage_events ADD COLUMN IF NOT EXISTS
                    client_round_trip_ms INTEGER;
                ALTER TABLE intelligence_usage_events ADD COLUMN IF NOT EXISTS
                    client_render_ms INTEGER;
                ALTER TABLE intelligence_usage_events ADD COLUMN IF NOT EXISTS
                    client_end_to_end_ms INTEGER;
                ALTER TABLE intelligence_usage_events ADD COLUMN IF NOT EXISTS
                    transport_ingress_ms INTEGER;

                CREATE TABLE IF NOT EXISTS intelligence_transcript_events (
                    request_id VARCHAR PRIMARY KEY,
                    occurred_at TIMESTAMPTZ NOT NULL,
                    user_hash VARCHAR NOT NULL,
                    messages_json JSON NOT NULL,
                    context_json JSON NOT NULL,
                    response_json JSON,
                    raw_response_text VARCHAR NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_intelligence_transcript_occurred
                    ON intelligence_transcript_events(occurred_at);

                CREATE TABLE IF NOT EXISTS sync_runs (
                    id VARCHAR PRIMARY KEY,
                    provider VARCHAR NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL,
                    completed_at TIMESTAMPTZ,
                    status VARCHAR NOT NULL,
                    resource_count INTEGER NOT NULL DEFAULT 0,
                    message VARCHAR NOT NULL DEFAULT ''
                );
                ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS trigger VARCHAR DEFAULT 'manual';
                ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS stage VARCHAR DEFAULT '';
                ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS stage_message VARCHAR DEFAULT '';
                ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
                ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS requested_sources_json JSON
                    DEFAULT '["inventory","advisor","intelligence","policy","cost"]';

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
                    message VARCHAR NOT NULL DEFAULT ''
                );
                ALTER TABLE sync_source_runs ADD COLUMN IF NOT EXISTS
                    last_attempt_at TIMESTAMPTZ;
                ALTER TABLE sync_source_runs ADD COLUMN IF NOT EXISTS
                    status_code INTEGER;
                ALTER TABLE sync_source_runs ADD COLUMN IF NOT EXISTS
                    retry_after_seconds DOUBLE;
                ALTER TABLE sync_source_runs ADD COLUMN IF NOT EXISTS
                    next_retry_at TIMESTAMPTZ;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_source_run
                    ON sync_source_runs(sync_id, source, scope_id);

                CREATE TABLE IF NOT EXISTS resource_snapshots (
                    snapshot_id VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    name VARCHAR NOT NULL,
                    resource_type VARCHAR NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    subscription_name VARCHAR NOT NULL,
                    resource_group VARCHAR NOT NULL,
                    region VARCHAR NOT NULL,
                    kind VARCHAR NOT NULL,
                    sku VARCHAR NOT NULL,
                    provisioning_state VARCHAR NOT NULL,
                    managed_by VARCHAR NOT NULL,
                    tags_json JSON NOT NULL,
                    estimated_monthly_cost DOUBLE,
                    cost_source VARCHAR,
                    utilization_percent DOUBLE,
                    utilization_source VARCHAR,
                    opportunity_kind VARCHAR,
                    opportunity_reason VARCHAR,
                    estimated_monthly_savings DOUBLE,
                    raw_json JSON NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cost_snapshots (
                    snapshot_id VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    period_start DATE NOT NULL,
                    period_end DATE NOT NULL,
                    cost_type VARCHAR NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    amount DOUBLE NOT NULL,
                    currency VARCHAR NOT NULL,
                    source VARCHAR NOT NULL
                );

                CREATE TABLE IF NOT EXISTS commitment_cost_snapshots (
                    snapshot_id VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    period_start DATE NOT NULL,
                    period_end DATE NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    meter_id VARCHAR NOT NULL,
                    pricing_model VARCHAR NOT NULL,
                    amount DOUBLE NOT NULL,
                    currency VARCHAR NOT NULL,
                    source VARCHAR NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_cost_history (
                    snapshot_id VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    usage_date DATE NOT NULL,
                    cost_type VARCHAR NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    service_name VARCHAR NOT NULL,
                    amount DOUBLE NOT NULL,
                    currency VARCHAR NOT NULL,
                    source VARCHAR NOT NULL,
                    PRIMARY KEY (
                        usage_date, cost_type, subscription_id,
                        resource_id, service_name, currency
                    )
                );

                CREATE TABLE IF NOT EXISTS monthly_cost_history (
                    snapshot_id VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    month DATE NOT NULL,
                    cost_type VARCHAR NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    amount DOUBLE NOT NULL,
                    currency VARCHAR NOT NULL,
                    source VARCHAR NOT NULL,
                    PRIMARY KEY (month, cost_type, subscription_id, currency)
                );

                CREATE TABLE IF NOT EXISTS reservation_snapshots (
                    snapshot_id VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    reservation_id VARCHAR NOT NULL,
                    order_id VARCHAR NOT NULL DEFAULT '',
                    display_name VARCHAR NOT NULL DEFAULT '',
                    sku VARCHAR NOT NULL DEFAULT '',
                    resource_type VARCHAR NOT NULL DEFAULT '',
                    region VARCHAR NOT NULL DEFAULT '',
                    quantity INTEGER NOT NULL DEFAULT 0,
                    term VARCHAR NOT NULL DEFAULT '',
                    scope_type VARCHAR NOT NULL DEFAULT '',
                    state VARCHAR NOT NULL DEFAULT '',
                    expiry_date DATE,
                    utilization_1d DOUBLE,
                    utilization_7d DOUBLE,
                    utilization_30d DOUBLE
                );

                CREATE TABLE IF NOT EXISTS price_sheet_current (
                    meter_id VARCHAR NOT NULL DEFAULT '',
                    meter_name VARCHAR NOT NULL DEFAULT '',
                    service_family VARCHAR NOT NULL DEFAULT '',
                    product VARCHAR NOT NULL DEFAULT '',
                    sku_id VARCHAR NOT NULL DEFAULT '',
                    unit_of_measure VARCHAR NOT NULL DEFAULT '',
                    price_type VARCHAR NOT NULL DEFAULT '',
                    unit_price DOUBLE,
                    base_price DOUBLE,
                    market_price DOUBLE,
                    currency VARCHAR NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS
                reservation_recommendation_snapshots (
                    snapshot_id VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    subscription_name VARCHAR NOT NULL DEFAULT '',
                    scope VARCHAR NOT NULL DEFAULT '',
                    resource_type VARCHAR NOT NULL DEFAULT '',
                    sku VARCHAR NOT NULL DEFAULT '',
                    region VARCHAR NOT NULL DEFAULT '',
                    term VARCHAR NOT NULL DEFAULT '',
                    look_back VARCHAR NOT NULL DEFAULT '',
                    recommended_quantity DOUBLE NOT NULL DEFAULT 0,
                    cost_without_commitment DOUBLE,
                    cost_with_commitment DOUBLE,
                    net_savings DOUBLE
                );

                CREATE TABLE IF NOT EXISTS focus_import_runs (
                    run_id VARCHAR PRIMARY KEY,
                    started_at TIMESTAMPTZ NOT NULL,
                    completed_at TIMESTAMPTZ,
                    status VARCHAR NOT NULL,
                    manifest_count INTEGER NOT NULL DEFAULT 0,
                    charge_count BIGINT NOT NULL DEFAULT 0,
                    message VARCHAR NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS focus_export_manifests (
                    manifest_id VARCHAR PRIMARY KEY,
                    import_run_id VARCHAR NOT NULL,
                    manifest_path VARCHAR NOT NULL,
                    export_name VARCHAR NOT NULL,
                    export_run_id VARCHAR NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    subscription_name VARCHAR NOT NULL,
                    period_start DATE NOT NULL,
                    period_end DATE NOT NULL,
                    submitted_at TIMESTAMPTZ,
                    imported_at TIMESTAMPTZ NOT NULL,
                    status VARCHAR NOT NULL,
                    data_version VARCHAR NOT NULL,
                    row_count BIGINT NOT NULL DEFAULT 0,
                    byte_count BIGINT NOT NULL DEFAULT 0,
                    currency VARCHAR NOT NULL DEFAULT '',
                    billed_cost DOUBLE NOT NULL DEFAULT 0,
                    effective_cost DOUBLE NOT NULL DEFAULT 0,
                    message VARCHAR NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_focus_manifest_path
                    ON focus_export_manifests(manifest_path);
                CREATE INDEX IF NOT EXISTS idx_focus_manifest_scope
                    ON focus_export_manifests(
                        subscription_id, period_start, period_end, imported_at
                    );

                CREATE TABLE IF NOT EXISTS focus_cost_charges (
                    charge_id VARCHAR PRIMARY KEY,
                    manifest_id VARCHAR NOT NULL,
                    charge_period_start TIMESTAMPTZ NOT NULL,
                    charge_period_end TIMESTAMPTZ,
                    billing_period_start TIMESTAMPTZ,
                    billing_period_end TIMESTAMPTZ,
                    billed_cost DOUBLE NOT NULL,
                    effective_cost DOUBLE NOT NULL,
                    contracted_cost DOUBLE,
                    list_cost DOUBLE,
                    billing_currency VARCHAR NOT NULL,
                    charge_category VARCHAR NOT NULL,
                    charge_class VARCHAR NOT NULL,
                    charge_frequency VARCHAR NOT NULL,
                    charge_description VARCHAR NOT NULL,
                    pricing_category VARCHAR NOT NULL,
                    consumed_quantity DOUBLE,
                    consumed_unit VARCHAR NOT NULL,
                    pricing_quantity DOUBLE,
                    pricing_unit VARCHAR NOT NULL,
                    contracted_unit_price DOUBLE,
                    list_unit_price DOUBLE,
                    commitment_discount_id VARCHAR NOT NULL,
                    commitment_discount_name VARCHAR NOT NULL,
                    commitment_discount_category VARCHAR NOT NULL,
                    commitment_discount_type VARCHAR NOT NULL,
                    service_category VARCHAR NOT NULL,
                    service_name VARCHAR NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    resource_name VARCHAR NOT NULL,
                    resource_type VARCHAR NOT NULL,
                    resource_group VARCHAR NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    subscription_name VARCHAR NOT NULL,
                    provider_name VARCHAR NOT NULL,
                    publisher_name VARCHAR NOT NULL,
                    region_name VARCHAR NOT NULL,
                    sku_id VARCHAR NOT NULL,
                    sku_price_id VARCHAR NOT NULL,
                    meter_id VARCHAR NOT NULL,
                    meter_name VARCHAR NOT NULL,
                    meter_category VARCHAR NOT NULL,
                    meter_subcategory VARCHAR NOT NULL,
                    tags_json JSON NOT NULL,
                    raw_json JSON NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_focus_charge_period
                    ON focus_cost_charges(
                        subscription_id, charge_period_start, service_name
                    );

                CREATE TABLE IF NOT EXISTS cost_history_runs (
                    run_id VARCHAR PRIMARY KEY,
                    started_at TIMESTAMPTZ NOT NULL,
                    completed_at TIMESTAMPTZ,
                    status VARCHAR NOT NULL,
                    expected_scopes INTEGER NOT NULL,
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
                    ON cost_history_scope_runs(
                        run_id, subscription_id, cost_type
                    );

                CREATE TABLE IF NOT EXISTS cost_history_request_attempts (
                    attempt_id VARCHAR PRIMARY KEY,
                    run_id VARCHAR NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    cost_type VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    status VARCHAR NOT NULL,
                    status_code INTEGER,
                    retry_after_seconds DOUBLE,
                    message VARCHAR NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_cost_history_request_scope
                    ON cost_history_request_attempts(
                        run_id, subscription_id, cost_type, observed_at
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
                    source VARCHAR NOT NULL DEFAULT
                        'azure_cost_details_report',
                    PRIMARY KEY (
                        subscription_id, cost_type, period_start
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_cost_details_backfill_status
                    ON cost_details_backfill_scopes(
                        status, next_retry_at, last_attempt_at
                    );

                CREATE TABLE IF NOT EXISTS cost_anomaly_runs (
                    run_id VARCHAR PRIMARY KEY,
                    evaluated_at TIMESTAMPTZ NOT NULL,
                    status VARCHAR NOT NULL,
                    evaluation_date DATE,
                    evaluated_count INTEGER NOT NULL,
                    anomaly_count INTEGER NOT NULL,
                    warming_count INTEGER NOT NULL,
                    message VARCHAR NOT NULL,
                    method_version VARCHAR NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cost_anomaly_snapshots (
                    run_id VARCHAR NOT NULL,
                    evaluated_at TIMESTAMPTZ NOT NULL,
                    evaluation_date DATE NOT NULL,
                    cost_type VARCHAR NOT NULL,
                    scope_type VARCHAR NOT NULL,
                    scope_id VARCHAR NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    resource_name VARCHAR NOT NULL,
                    resource_type VARCHAR NOT NULL,
                    resource_group VARCHAR NOT NULL,
                    service_name VARCHAR NOT NULL,
                    current_amount DOUBLE NOT NULL,
                    baseline_points INTEGER NOT NULL,
                    baseline_median DOUBLE,
                    mad DOUBLE,
                    k_score DOUBLE,
                    previous_week_amount DOUBLE,
                    absolute_change DOUBLE,
                    percent_change DOUBLE,
                    status VARCHAR NOT NULL,
                    severity VARCHAR NOT NULL,
                    currency VARCHAR NOT NULL,
                    reason VARCHAR NOT NULL,
                    method_version VARCHAR NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cost_anomaly_reviews (
                    run_id VARCHAR NOT NULL,
                    cost_type VARCHAR NOT NULL,
                    scope_type VARCHAR NOT NULL,
                    scope_id VARCHAR NOT NULL,
                    review_status VARCHAR NOT NULL,
                    note VARCHAR NOT NULL DEFAULT '',
                    updated_by VARCHAR NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (run_id, cost_type, scope_type, scope_id)
                );

                CREATE TABLE IF NOT EXISTS policy_posture_snapshots (
                    snapshot_id VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    subscription_name VARCHAR NOT NULL,
                    assignment_id VARCHAR NOT NULL,
                    assignment_name VARCHAR NOT NULL,
                    evaluated_count INTEGER NOT NULL,
                    compliant_count INTEGER NOT NULL,
                    non_compliant_count INTEGER NOT NULL,
                    exempt_count INTEGER NOT NULL,
                    unknown_count INTEGER NOT NULL,
                    resource_count INTEGER NOT NULL,
                    definition_count INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS policy_resource_snapshots (
                    snapshot_id VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    subscription_name VARCHAR NOT NULL,
                    assignment_id VARCHAR NOT NULL,
                    assignment_name VARCHAR NOT NULL,
                    definition_id VARCHAR NOT NULL,
                    definition_name VARCHAR NOT NULL,
                    compliance_state VARCHAR NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    resource_name VARCHAR NOT NULL,
                    resource_type VARCHAR NOT NULL,
                    region VARCHAR NOT NULL,
                    exemption_id VARCHAR NOT NULL,
                    evaluated_at VARCHAR NOT NULL
                );

                CREATE TABLE IF NOT EXISTS retail_price_snapshots (
                    snapshot_id VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    arm_region_name VARCHAR NOT NULL,
                    arm_sku_name VARCHAR NOT NULL,
                    operating_system VARCHAR NOT NULL,
                    license_model VARCHAR NOT NULL,
                    price_profile VARCHAR NOT NULL,
                    currency VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    hourly_price DOUBLE,
                    monthly_price DOUBLE,
                    monthly_compute_price DOUBLE,
                    monthly_license_price DOUBLE,
                    monthly_ri_1y DOUBLE,
                    ri_1y_upfront DOUBLE,
                    monthly_sp_1y DOUBLE,
                    hours_per_month DOUBLE NOT NULL,
                    meter_id VARCHAR NOT NULL,
                    meter_name VARCHAR NOT NULL,
                    product_name VARCHAR NOT NULL,
                    sku_name VARCHAR NOT NULL,
                    unit_of_measure VARCHAR NOT NULL,
                    effective_start_date TIMESTAMPTZ,
                    candidate_count INTEGER NOT NULL,
                    source VARCHAR NOT NULL,
                    source_url VARCHAR NOT NULL,
                    message VARCHAR NOT NULL,
                    raw_json JSON NOT NULL
                );
                ALTER TABLE retail_price_snapshots ADD COLUMN IF NOT EXISTS
                    monthly_compute_price DOUBLE;
                ALTER TABLE retail_price_snapshots ADD COLUMN IF NOT EXISTS
                    monthly_license_price DOUBLE;
                ALTER TABLE retail_price_snapshots ADD COLUMN IF NOT EXISTS
                    monthly_ri_1y DOUBLE;
                ALTER TABLE retail_price_snapshots ADD COLUMN IF NOT EXISTS
                    ri_1y_upfront DOUBLE;
                ALTER TABLE retail_price_snapshots ADD COLUMN IF NOT EXISTS
                    monthly_sp_1y DOUBLE;

                CREATE TABLE IF NOT EXISTS advisor_recommendation_snapshots (
                    snapshot_id VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    recommendation_id VARCHAR NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    subscription_name VARCHAR NOT NULL,
                    resource_type VARCHAR NOT NULL,
                    category VARCHAR NOT NULL,
                    impact VARCHAR NOT NULL,
                    problem VARCHAR NOT NULL,
                    solution VARCHAR NOT NULL,
                    savings_amount DOUBLE,
                    savings_currency VARCHAR NOT NULL,
                    raw_json JSON NOT NULL,
                    annual_savings_amount DOUBLE,
                    recommendation_type_id VARCHAR NOT NULL DEFAULT '',
                    current_sku VARCHAR NOT NULL DEFAULT '',
                    recommended_sku VARCHAR NOT NULL DEFAULT '',
                    last_updated TIMESTAMPTZ,
                    learn_more_link VARCHAR NOT NULL DEFAULT ''
                );

                ALTER TABLE advisor_recommendation_snapshots
                    ADD COLUMN IF NOT EXISTS annual_savings_amount DOUBLE;
                ALTER TABLE advisor_recommendation_snapshots
                    ADD COLUMN IF NOT EXISTS recommendation_type_id VARCHAR
                    DEFAULT '';
                ALTER TABLE advisor_recommendation_snapshots
                    ADD COLUMN IF NOT EXISTS current_sku VARCHAR DEFAULT '';
                ALTER TABLE advisor_recommendation_snapshots
                    ADD COLUMN IF NOT EXISTS recommended_sku VARCHAR DEFAULT '';
                ALTER TABLE advisor_recommendation_snapshots
                    ADD COLUMN IF NOT EXISTS last_updated TIMESTAMPTZ;
                ALTER TABLE advisor_recommendation_snapshots
                    ADD COLUMN IF NOT EXISTS learn_more_link VARCHAR DEFAULT '';

                CREATE TABLE IF NOT EXISTS rule_opportunity_snapshots (
                    snapshot_id VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    finding_id VARCHAR NOT NULL,
                    rule_id VARCHAR NOT NULL,
                    source VARCHAR NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    related_resource_id VARCHAR NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    subscription_name VARCHAR NOT NULL,
                    resource_type VARCHAR NOT NULL,
                    resource_group VARCHAR NOT NULL,
                    region VARCHAR NOT NULL,
                    category VARCHAR NOT NULL,
                    impact VARCHAR NOT NULL,
                    confidence VARCHAR NOT NULL,
                    title VARCHAR NOT NULL,
                    reason VARCHAR NOT NULL,
                    evidence_json JSON NOT NULL,
                    estimated_monthly_savings DOUBLE,
                    savings_currency VARCHAR NOT NULL,
                    rule_version VARCHAR NOT NULL
                );

                CREATE TABLE IF NOT EXISTS finops_toolkit_dataset_versions (
                    dataset VARCHAR NOT NULL,
                    toolkit_version VARCHAR NOT NULL,
                    upstream_commit VARCHAR NOT NULL,
                    source_url VARCHAR NOT NULL,
                    sha256 VARCHAR NOT NULL,
                    imported_at TIMESTAMPTZ NOT NULL,
                    row_count BIGINT NOT NULL,
                    license VARCHAR NOT NULL,
                    PRIMARY KEY (dataset, toolkit_version, sha256)
                );

                CREATE TABLE IF NOT EXISTS finops_toolkit_resource_types (
                    toolkit_version VARCHAR NOT NULL,
                    imported_at TIMESTAMPTZ NOT NULL,
                    resource_type VARCHAR NOT NULL,
                    singular_display_name VARCHAR NOT NULL,
                    plural_display_name VARCHAR NOT NULL,
                    lower_singular_display_name VARCHAR NOT NULL,
                    lower_plural_display_name VARCHAR NOT NULL,
                    is_preview BOOLEAN,
                    description VARCHAR NOT NULL,
                    icon VARCHAR NOT NULL,
                    links_json JSON NOT NULL
                );

                CREATE TABLE IF NOT EXISTS finops_toolkit_regions (
                    toolkit_version VARCHAR NOT NULL,
                    imported_at TIMESTAMPTZ NOT NULL,
                    original_value VARCHAR NOT NULL,
                    region_id VARCHAR NOT NULL,
                    region_name VARCHAR NOT NULL
                );

                CREATE TABLE IF NOT EXISTS finops_toolkit_services (
                    toolkit_version VARCHAR NOT NULL,
                    imported_at TIMESTAMPTZ NOT NULL,
                    consumed_service VARCHAR NOT NULL,
                    resource_type VARCHAR NOT NULL,
                    service_name VARCHAR NOT NULL,
                    service_category VARCHAR NOT NULL,
                    service_subcategory VARCHAR NOT NULL,
                    publisher_name VARCHAR NOT NULL,
                    publisher_type VARCHAR NOT NULL,
                    environment VARCHAR NOT NULL,
                    service_model VARCHAR NOT NULL
                );

                CREATE TABLE IF NOT EXISTS finops_toolkit_pricing_units (
                    toolkit_version VARCHAR NOT NULL,
                    imported_at TIMESTAMPTZ NOT NULL,
                    unit_of_measure VARCHAR NOT NULL,
                    account_types VARCHAR NOT NULL,
                    pricing_block_size DOUBLE,
                    distinct_units VARCHAR NOT NULL
                );

                CREATE TABLE IF NOT EXISTS finops_toolkit_commitment_eligibility (
                    toolkit_version VARCHAR NOT NULL,
                    imported_at TIMESTAMPTZ NOT NULL,
                    meter_id VARCHAR NOT NULL,
                    spend_eligibility VARCHAR NOT NULL,
                    usage_eligibility VARCHAR NOT NULL
                );

                CREATE TABLE IF NOT EXISTS source_sync_state (
                    snapshot_id VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    source VARCHAR NOT NULL,
                    scope_id VARCHAR NOT NULL,
                    row_count INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS telemetry_runs (
                    id VARCHAR PRIMARY KEY,
                    source VARCHAR NOT NULL,
                    trigger VARCHAR NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL,
                    completed_at TIMESTAMPTZ,
                    status VARCHAR NOT NULL,
                    processed_count INTEGER NOT NULL DEFAULT 0,
                    message VARCHAR NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS telemetry_metric_summaries (
                    run_id VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    source VARCHAR NOT NULL,
                    metric VARCHAR NOT NULL,
                    unit VARCHAR NOT NULL,
                    window_start TIMESTAMPTZ NOT NULL,
                    window_end TIMESTAMPTZ NOT NULL,
                    sample_count INTEGER NOT NULL,
                    coverage_percent DOUBLE NOT NULL,
                    average DOUBLE,
                    p95 DOUBLE,
                    maximum DOUBLE,
                    last_value DOUBLE,
                    last_observed_at TIMESTAMPTZ
                );
                ALTER TABLE telemetry_metric_summaries
                    ADD COLUMN IF NOT EXISTS aggregation_method VARCHAR
                    DEFAULT '';
                ALTER TABLE telemetry_metric_summaries
                    ADD COLUMN IF NOT EXISTS lineage_json JSON DEFAULT '{}';

                -- Deliberately NO primary key: an ART index over millions
                -- of composite varchar keys must be memory-resident for
                -- every insert, and at this table's steady-state size that
                -- alone exceeded the DuckDB memory cap -- every
                -- LogicMonitor metrics import died OOM ("failed to pin
                -- block", 2026-08-01/02) once the table filled out.
                -- Idempotency is explicit instead: store_telemetry_samples
                -- deletes each batch's (source, resource, window) slice
                -- before inserting, which is a spillable sequential scan.
                CREATE TABLE IF NOT EXISTS telemetry_metric_samples (
                    run_id VARCHAR NOT NULL,
                    ingested_at TIMESTAMPTZ NOT NULL,
                    source VARCHAR NOT NULL,
                    source_resource_id VARCHAR NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    metric VARCHAR NOT NULL,
                    unit VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    value DOUBLE NOT NULL,
                    lineage_json JSON NOT NULL
                );

                CREATE TABLE IF NOT EXISTS telemetry_collection_checkpoints (
                    source VARCHAR NOT NULL,
                    source_resource_id VARCHAR NOT NULL,
                    stream VARCHAR NOT NULL,
                    collected_through TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    status VARCHAR NOT NULL,
                    message VARCHAR NOT NULL,
                    PRIMARY KEY (source, source_resource_id, stream)
                );

                CREATE TABLE IF NOT EXISTS resource_source_matches (
                    run_id VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    source VARCHAR NOT NULL,
                    source_resource_id VARCHAR NOT NULL,
                    source_name VARCHAR NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    method VARCHAR NOT NULL,
                    confidence VARCHAR NOT NULL,
                    details_json JSON NOT NULL
                );

                CREATE TABLE IF NOT EXISTS telemetry_resource_attempts (
                    run_id VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    source VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    metric_count INTEGER NOT NULL,
                    message VARCHAR NOT NULL
                );

                CREATE TABLE IF NOT EXISTS opportunity_confidence_snapshots (
                    snapshot_id VARCHAR NOT NULL,
                    computed_at TIMESTAMPTZ NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    opportunity_type VARCHAR NOT NULL,
                    first_seen TIMESTAMPTZ NOT NULL,
                    last_seen TIMESTAMPTZ NOT NULL,
                    consecutive_count INTEGER NOT NULL,
                    reappeared_after_remediation BOOLEAN NOT NULL,
                    confidence DOUBLE NOT NULL,
                    confidence_label VARCHAR NOT NULL,
                    factors_json JSON NOT NULL,
                    method_version VARCHAR NOT NULL
                );

                -- V2 intentionally leaves the original derived valuation
                -- table untouched. A production DuckDB FSST segment failure
                -- in that table must not make inventory, cost, telemetry, or
                -- recommendation source data unavailable.
                CREATE TABLE IF NOT EXISTS opportunity_valuation_snapshots_v2 (
                    snapshot_id VARCHAR NOT NULL,
                    computed_at TIMESTAMPTZ NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    opportunity_type VARCHAR NOT NULL,
                    source VARCHAR NOT NULL,
                    valuation_status VARCHAR NOT NULL,
                    monthly_gross DOUBLE,
                    monthly_risk_adjusted DOUBLE,
                    currency VARCHAR NOT NULL,
                    value_source VARCHAR NOT NULL,
                    valuation_basis VARCHAR NOT NULL,
                    cost_snapshot_id VARCHAR NOT NULL,
                    cost_type VARCHAR NOT NULL,
                    cost_period_start DATE,
                    cost_period_end DATE,
                    confidence DOUBLE,
                    method_version VARCHAR NOT NULL
                );
                ALTER TABLE opportunity_valuation_snapshots_v2
                    ADD COLUMN IF NOT EXISTS current_monthly_cost DOUBLE;
                ALTER TABLE opportunity_valuation_snapshots_v2
                    ADD COLUMN IF NOT EXISTS target_monthly_cost DOUBLE;
                ALTER TABLE opportunity_valuation_snapshots_v2
                    ADD COLUMN IF NOT EXISTS current_cost_basis VARCHAR DEFAULT '';
                ALTER TABLE opportunity_valuation_snapshots_v2
                    ADD COLUMN IF NOT EXISTS target_price_basis VARCHAR DEFAULT '';
                ALTER TABLE opportunity_valuation_snapshots_v2
                    ADD COLUMN IF NOT EXISTS target_price_snapshot_id VARCHAR DEFAULT '';
                ALTER TABLE opportunity_valuation_snapshots_v2
                    ADD COLUMN IF NOT EXISTS target_price_status VARCHAR DEFAULT '';
                ALTER TABLE opportunity_valuation_snapshots_v2
                    ADD COLUMN IF NOT EXISTS target_hourly_price DOUBLE;
                ALTER TABLE opportunity_valuation_snapshots_v2
                    ADD COLUMN IF NOT EXISTS target_hours_per_month DOUBLE;
                ALTER TABLE opportunity_valuation_snapshots_v2
                    ADD COLUMN IF NOT EXISTS target_meter_id VARCHAR DEFAULT '';
                ALTER TABLE opportunity_valuation_snapshots_v2
                    ADD COLUMN IF NOT EXISTS target_meter_name VARCHAR DEFAULT '';
                ALTER TABLE opportunity_valuation_snapshots_v2
                    ADD COLUMN IF NOT EXISTS target_product_name VARCHAR DEFAULT '';
                ALTER TABLE opportunity_valuation_snapshots_v2
                    ADD COLUMN IF NOT EXISTS target_price_effective_start TIMESTAMPTZ;
                ALTER TABLE opportunity_valuation_snapshots_v2
                    ADD COLUMN IF NOT EXISTS current_sku VARCHAR DEFAULT '';
                ALTER TABLE opportunity_valuation_snapshots_v2
                    ADD COLUMN IF NOT EXISTS target_sku VARCHAR DEFAULT '';
                ALTER TABLE opportunity_valuation_snapshots_v2
                    ADD COLUMN IF NOT EXISTS price_region VARCHAR DEFAULT '';
                ALTER TABLE opportunity_valuation_snapshots_v2
                    ADD COLUMN IF NOT EXISTS operating_system VARCHAR DEFAULT '';
                ALTER TABLE opportunity_valuation_snapshots_v2
                    ADD COLUMN IF NOT EXISTS license_model VARCHAR DEFAULT '';

                CREATE TABLE IF NOT EXISTS inventory_drift_runs (
                    snapshot_id VARCHAR PRIMARY KEY,
                    previous_snapshot_id VARCHAR NOT NULL,
                    computed_at TIMESTAMPTZ NOT NULL,
                    change_count INTEGER NOT NULL,
                    method_version VARCHAR NOT NULL
                );

                CREATE TABLE IF NOT EXISTS inventory_changes (
                    snapshot_id VARCHAR NOT NULL,
                    previous_snapshot_id VARCHAR NOT NULL,
                    computed_at TIMESTAMPTZ NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    resource_name VARCHAR NOT NULL,
                    resource_type VARCHAR NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    subscription_name VARCHAR NOT NULL,
                    resource_group VARCHAR NOT NULL,
                    region VARCHAR NOT NULL,
                    change_type VARCHAR NOT NULL,
                    from_fingerprint VARCHAR NOT NULL,
                    to_fingerprint VARCHAR NOT NULL,
                    details_json JSON NOT NULL,
                    method_version VARCHAR NOT NULL
                );

                CREATE TABLE IF NOT EXISTS inventory_change_anomalies (
                    snapshot_id VARCHAR NOT NULL,
                    computed_at TIMESTAMPTZ NOT NULL,
                    scope_type VARCHAR NOT NULL,
                    scope_id VARCHAR NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    resource_group VARCHAR NOT NULL,
                    change_type VARCHAR NOT NULL,
                    change_count INTEGER NOT NULL,
                    baseline_points INTEGER NOT NULL,
                    baseline_median DOUBLE,
                    mad DOUBLE,
                    k_score DOUBLE,
                    threshold_k DOUBLE NOT NULL,
                    status VARCHAR NOT NULL,
                    method_version VARCHAR NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rightsizing_recommendation_snapshots (
                    run_id VARCHAR NOT NULL,
                    computed_at TIMESTAMPTZ NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    resource_name VARCHAR NOT NULL,
                    subscription_id VARCHAR NOT NULL,
                    subscription_name VARCHAR NOT NULL,
                    resource_group VARCHAR NOT NULL,
                    region VARCHAR NOT NULL,
                    kind VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    current_sku VARCHAR NOT NULL,
                    target_sku VARCHAR NOT NULL,
                    evidence_window_days INTEGER NOT NULL,
                    coverage_flag VARCHAR NOT NULL,
                    telemetry_source VARCHAR NOT NULL,
                    cpu_p95 DOUBLE,
                    cpu_maximum DOUBLE,
                    network_in_p95 DOUBLE,
                    network_out_p95 DOUBLE,
                    metric_coverage_percent DOUBLE,
                    estimated_monthly_saving DOUBLE,
                    currency VARCHAR NOT NULL,
                    value_source VARCHAR NOT NULL,
                    reason VARCHAR NOT NULL,
                    evidence_json JSON NOT NULL,
                    method_version VARCHAR NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_resource_snapshot_id
                    ON resource_snapshots(snapshot_id);
                CREATE INDEX IF NOT EXISTS idx_resource_id
                    ON resource_snapshots(resource_id);
                CREATE INDEX IF NOT EXISTS idx_cost_resource_id
                    ON cost_snapshots(resource_id);
                CREATE INDEX IF NOT EXISTS idx_commitment_cost_meter
                    ON commitment_cost_snapshots(meter_id);
                CREATE INDEX IF NOT EXISTS idx_daily_cost_scope
                    ON daily_cost_history(
                        cost_type, subscription_id, usage_date
                    );
                CREATE INDEX IF NOT EXISTS idx_cost_anomaly_scope
                    ON cost_anomaly_snapshots(
                        run_id, status, scope_type, subscription_id
                    );
                CREATE INDEX IF NOT EXISTS idx_policy_posture_snapshot
                    ON policy_posture_snapshots(snapshot_id);
                CREATE INDEX IF NOT EXISTS idx_retail_price_key
                    ON retail_price_snapshots(
                        arm_region_name, arm_sku_name, price_profile, currency
                    );
                CREATE INDEX IF NOT EXISTS idx_advisor_resource_id
                    ON advisor_recommendation_snapshots(resource_id);
                CREATE INDEX IF NOT EXISTS idx_rule_opportunity_resource_id
                    ON rule_opportunity_snapshots(resource_id);
                CREATE INDEX IF NOT EXISTS idx_opportunity_confidence_resource_id
                    ON opportunity_confidence_snapshots(resource_id);
                CREATE INDEX IF NOT EXISTS idx_inventory_changes_snapshot
                    ON inventory_changes(snapshot_id);
                CREATE INDEX IF NOT EXISTS idx_inventory_changes_resource
                    ON inventory_changes(resource_id);
                CREATE INDEX IF NOT EXISTS idx_rightsizing_resource
                    ON rightsizing_recommendation_snapshots(resource_id);

                INSERT INTO source_sync_state
                SELECT
                    snapshot_id,
                    observed_at,
                    'AzureResourceGraph',
                    'configured-subscriptions',
                    row_count
                FROM (
                    SELECT
                        arg_max(snapshot_id, observed_at) AS snapshot_id,
                        max(observed_at) AS observed_at,
                        count(*) AS row_count
                    FROM resource_snapshots
                ) AS latest
                WHERE snapshot_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM source_sync_state
                      WHERE source = 'AzureResourceGraph'
                        AND scope_id = 'configured-subscriptions'
                  );

                -- The four hottest "current" projections are materialized
                -- TABLES, not VIEWs. A VIEW recomputes arg_max/window functions
                -- on every reference; cost_report and overview reference
                -- resources_current 10+ times per request, and the Reports
                -- page trace showed 96-second TTFB entirely in DuckDB. A
                -- materialized table is a single scan/index lookup instead.
                -- refresh_current_views() rebuilds them after snapshot writes.
                -- The remaining views are left as VIEWs (cold or compute-side).

                CREATE TABLE IF NOT EXISTS resources_current AS
                WITH latest AS (
                    SELECT arg_max(snapshot_id, observed_at) AS snapshot_id
                    FROM source_sync_state
                    WHERE source = 'AzureResourceGraph'
                      AND scope_id = 'configured-subscriptions'
                )
                SELECT resource.*
                FROM resource_snapshots AS resource
                JOIN latest ON latest.snapshot_id = resource.snapshot_id;

                CREATE TABLE IF NOT EXISTS costs_current AS
                WITH latest AS (
                    SELECT
                        source,
                        scope_id,
                        arg_max(snapshot_id, observed_at) AS snapshot_id
                    FROM source_sync_state
                    WHERE source IN ('ActualCost', 'AmortizedCost')
                    GROUP BY source, scope_id
                )
                SELECT cost.*
                FROM cost_snapshots AS cost
                JOIN latest
                  ON latest.snapshot_id = cost.snapshot_id
                 AND latest.source = cost.cost_type
                 AND latest.scope_id = cost.subscription_id;

                CREATE TABLE IF NOT EXISTS commitment_costs_current AS
                WITH latest AS (
                    SELECT
                        scope_id,
                        arg_max(snapshot_id, observed_at) AS snapshot_id
                    FROM source_sync_state
                    WHERE source = 'CommitmentCoverage'
                    GROUP BY scope_id
                )
                SELECT cost.*
                FROM commitment_cost_snapshots AS cost
                JOIN latest
                  ON latest.snapshot_id = cost.snapshot_id
                 AND latest.scope_id = cost.subscription_id;

                CREATE TABLE IF NOT EXISTS policy_posture_current AS
                WITH latest AS (
                    SELECT arg_max(snapshot_id, observed_at) AS snapshot_id
                    FROM source_sync_state
                    WHERE source = 'AzurePolicy'
                      AND scope_id = 'configured-subscriptions'
                )
                SELECT posture.*
                FROM policy_posture_snapshots AS posture
                JOIN latest ON latest.snapshot_id = posture.snapshot_id;

                CREATE OR REPLACE VIEW focus_manifests_current AS
                SELECT * EXCLUDE (rank)
                FROM (
                    SELECT *, row_number() OVER (
                        PARTITION BY subscription_id, period_start, period_end
                        ORDER BY imported_at DESC
                    ) AS rank
                    FROM focus_export_manifests
                    WHERE status = 'imported'
                )
                WHERE rank = 1;

                CREATE OR REPLACE VIEW focus_cost_current AS
                SELECT charge.*
                FROM focus_cost_charges AS charge
                JOIN focus_manifests_current AS manifest
                  ON manifest.manifest_id = charge.manifest_id;

                CREATE OR REPLACE VIEW cost_anomalies_current AS
                WITH latest AS (
                    SELECT arg_max(run_id, evaluated_at) AS run_id
                    FROM cost_anomaly_runs
                    WHERE status = 'succeeded'
                )
                SELECT anomaly.*
                FROM cost_anomaly_snapshots AS anomaly
                JOIN latest ON latest.run_id = anomaly.run_id;

                CREATE OR REPLACE VIEW reservation_inventory_current AS
                WITH latest AS (
                    SELECT arg_max(snapshot_id, observed_at) AS snapshot_id
                    FROM reservation_snapshots
                )
                SELECT reservation.*
                FROM reservation_snapshots AS reservation
                JOIN latest ON latest.snapshot_id = reservation.snapshot_id;

                CREATE OR REPLACE VIEW
                reservation_recommendations_current AS
                WITH latest AS (
                    SELECT arg_max(snapshot_id, observed_at) AS snapshot_id
                    FROM reservation_recommendation_snapshots
                )
                SELECT recommendation.*
                FROM reservation_recommendation_snapshots AS recommendation
                JOIN latest
                  ON latest.snapshot_id = recommendation.snapshot_id;

                CREATE OR REPLACE VIEW policy_resources_current AS
                WITH latest AS (
                    SELECT arg_max(snapshot_id, observed_at) AS snapshot_id
                    FROM policy_resource_snapshots
                )
                SELECT resource.*
                FROM policy_resource_snapshots AS resource
                JOIN latest ON latest.snapshot_id = resource.snapshot_id;

                CREATE OR REPLACE VIEW retail_price_attempts_current AS
                WITH ranked AS (
                    SELECT price.*,
                        row_number() OVER (
                            PARTITION BY
                                arm_region_name, arm_sku_name,
                                price_profile, currency
                            ORDER BY observed_at DESC, snapshot_id DESC
                        ) AS rank
                    FROM retail_price_snapshots AS price
                )
                SELECT * EXCLUDE (rank) FROM ranked WHERE rank = 1;

                CREATE OR REPLACE VIEW retail_prices_current AS
                WITH ranked AS (
                    SELECT price.*,
                        row_number() OVER (
                            PARTITION BY
                                arm_region_name, arm_sku_name,
                                price_profile, currency
                            ORDER BY observed_at DESC, snapshot_id DESC
                        ) AS rank
                    FROM retail_price_snapshots AS price
                    WHERE status = 'matched'
                )
                SELECT * EXCLUDE (rank) FROM ranked WHERE rank = 1;



                CREATE OR REPLACE VIEW telemetry_metric_summaries_current AS
                WITH ranked AS (
                    SELECT summary.*,
                        row_number() OVER (
                            PARTITION BY summary.resource_id, summary.source,
                                summary.metric
                            ORDER BY
                                CASE WHEN date_diff(
                                    'hour',
                                    summary.window_start,
                                    summary.window_end
                                ) >= 312 THEN 1 ELSE 2 END,
                                summary.observed_at DESC,
                                summary.run_id DESC
                        ) AS rank
                    FROM telemetry_metric_summaries AS summary
                    JOIN telemetry_runs AS run ON run.id = summary.run_id
                    WHERE run.status = 'succeeded'
                )
                SELECT * EXCLUDE (rank) FROM ranked WHERE rank = 1;

                CREATE OR REPLACE VIEW resource_source_matches_current AS
                WITH ranked AS (
                    SELECT match.*,
                        row_number() OVER (
                            PARTITION BY match.source, match.source_resource_id
                            ORDER BY match.observed_at DESC, match.run_id DESC
                        ) AS rank
                    FROM resource_source_matches AS match
                    JOIN telemetry_runs AS run ON run.id = match.run_id
                    WHERE run.status = 'succeeded'
                )
                SELECT * EXCLUDE (rank) FROM ranked WHERE rank = 1;

                CREATE OR REPLACE VIEW telemetry_resource_attempts_current AS
                WITH ranked AS (
                    SELECT attempt.*,
                        row_number() OVER (
                            PARTITION BY attempt.resource_id, attempt.source
                            ORDER BY attempt.observed_at DESC,
                                attempt.run_id DESC
                        ) AS rank
                    FROM telemetry_resource_attempts AS attempt
                    JOIN telemetry_runs AS run ON run.id = attempt.run_id
                    WHERE run.status = 'succeeded'
                )
                SELECT * EXCLUDE (rank) FROM ranked WHERE rank = 1;



                CREATE OR REPLACE VIEW inventory_changes_current AS
                WITH latest AS (
                    SELECT arg_max(snapshot_id, computed_at) AS snapshot_id
                    FROM inventory_drift_runs
                )
                SELECT change.*
                FROM inventory_changes AS change
                JOIN latest ON latest.snapshot_id = change.snapshot_id;

                CREATE OR REPLACE VIEW inventory_change_anomalies_current AS
                WITH latest AS (
                    SELECT arg_max(snapshot_id, computed_at) AS snapshot_id
                    FROM inventory_drift_runs
                )
                SELECT anomaly.*
                FROM inventory_change_anomalies AS anomaly
                JOIN latest ON latest.snapshot_id = anomaly.snapshot_id;

                CREATE OR REPLACE VIEW rightsizing_recommendations_current AS
                WITH latest AS (
                    SELECT arg_max(run_id, computed_at) AS run_id
                    FROM rightsizing_recommendation_snapshots
                )
                SELECT recommendation.*
                FROM rightsizing_recommendation_snapshots AS recommendation
                JOIN latest ON latest.run_id = recommendation.run_id;
                -- Sargable indexes for the cost-report hot path. Without these,
                -- daily_cost_history (PK leads with usage_date) and cost_snapshots
                -- are full-scanned when filtered by cost_type + subscription_id.
                CREATE INDEX IF NOT EXISTS idx_daily_cost_history_type_sub_date
                    ON daily_cost_history(cost_type, subscription_id, usage_date);
                CREATE INDEX IF NOT EXISTS idx_cost_snapshots_type_sub
                    ON cost_snapshots(cost_type, subscription_id);
                CREATE INDEX IF NOT EXISTS idx_resource_snapshots_snapshot
                    ON resource_snapshots(snapshot_id);
                CREATE INDEX IF NOT EXISTS idx_policy_posture_snapshots_snapshot
                    ON policy_posture_snapshots(snapshot_id);
                """
            )
            # Migrate existing databases: if resources_current / costs_current /
            # commitment_costs_current / policy_posture_current are still VIEWs
            # (pre-materialization), drop them so the CREATE TABLE above takes
            # effect. DuckDB does not let CREATE TABLE IF NOT EXISTS replace
            # an existing VIEW of the same name.
            for table_name in (
                "resources_current",
                "costs_current",
                "commitment_costs_current",
                "policy_posture_current",
            ):
                kind = db.execute(
                    "SELECT table_type FROM information_schema.tables "
                    "WHERE table_name = ?",
                    [table_name],
                ).fetchone()
                if kind and kind[0] == "VIEW":
                    db.execute(f"DROP VIEW {table_name}")
            # (Re)create the four materialized tables with current data.
            self._refresh_materialized_tables_internal(db)
            valuation_index_migration = "20260725_rebuild_opportunity_valuation_index"
            migration_applied = db.execute(
                "SELECT count(*) FROM schema_migrations WHERE version = ?",
                [valuation_index_migration],
            ).fetchone()[0]
            if not migration_applied:
                # DuckDB can retain an inconsistent ART index when columns are
                # added to an already-indexed table. Remove the derived index
                # after the valuation schema upgrade, but do not rebuild it
                # during application startup. Rebuilding requires a full scan
                # of historic compressed resource IDs and can terminate the
                # native DuckDB process when an older FSST segment is damaged.
                # The index is optional: valuation joins remain correct without
                # it, and governed health checks can report the underlying data
                # condition after the application is available.
                db.execute(
                    "DROP INDEX IF EXISTS idx_opportunity_valuation_resource_id"
                )
                db.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)",
                    [valuation_index_migration, utc_now()],
                )
            focus_subscription_migration = "20260729_normalize_focus_subscription_id"
            focus_migration_applied = db.execute(
                "SELECT count(*) FROM schema_migrations WHERE version = ?",
                [focus_subscription_migration],
            ).fetchone()[0]
            if not focus_migration_applied:
                # Azure FOCUS exports carry SubAccountId as the full ARM path,
                # so imported charges and their projected daily history keyed on
                # "/subscriptions/<guid>" while inventory, current cost,
                # manifests, budgets, and anomaly baselines key on the bare
                # GUID. The same subscription therefore appeared as two scopes
                # and FOCUS cost could not join to inventory.
                #
                # FOCUS is authoritative for periods it covers, so where
                # normalization would collide with a Cost Management row for
                # the same logical key, the Cost Management row is removed
                # first and the FOCUS row wins.
                db.execute(
                    """
                    DELETE FROM daily_cost_history AS legacy
                    WHERE legacy.source <> 'azure_focus_export'
                      AND EXISTS (
                          SELECT 1 FROM daily_cost_history AS focus
                          WHERE focus.source = 'azure_focus_export'
                            AND focus.subscription_id LIKE '/subscriptions/%'
                            AND regexp_replace(
                                    focus.subscription_id, '^/subscriptions/', ''
                                ) = legacy.subscription_id
                            AND focus.usage_date = legacy.usage_date
                            AND focus.cost_type = legacy.cost_type
                            AND focus.resource_id = legacy.resource_id
                            AND focus.service_name = legacy.service_name
                            AND focus.currency = legacy.currency
                      )
                    """
                )
                for table in ("daily_cost_history", "focus_cost_charges"):
                    db.execute(
                        f"""
                        UPDATE {table}
                        SET subscription_id = regexp_replace(
                            subscription_id, '^/subscriptions/', ''
                        )
                        WHERE subscription_id LIKE '/subscriptions/%'
                        """
                    )
                db.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)",
                    [focus_subscription_migration, utc_now()],
                )
            samples_pk_migration = "20260802_drop_telemetry_samples_pk"
            samples_pk_applied = db.execute(
                "SELECT count(*) FROM schema_migrations WHERE version = ?",
                [samples_pk_migration],
            ).fetchone()[0]
            if not samples_pk_applied:
                has_pk = db.execute(
                    """
                    SELECT count(*) FROM duckdb_constraints()
                    WHERE table_name = 'telemetry_metric_samples'
                      AND constraint_type = 'PRIMARY KEY'
                    """
                ).fetchone()[0]
                if has_pk:
                    # Rebuild without the PK (see the schema comment above):
                    # a sequential CTAS streams within the memory cap where
                    # loading the old ART index cannot.
                    db.execute(
                        "CREATE TABLE telemetry_metric_samples_rebuild AS "
                        "SELECT * FROM telemetry_metric_samples"
                    )
                    db.execute("DROP TABLE telemetry_metric_samples")
                    db.execute(
                        "ALTER TABLE telemetry_metric_samples_rebuild "
                        "RENAME TO telemetry_metric_samples"
                    )
                db.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)",
                    [samples_pk_migration, utc_now()],
                )
            legacy_valuation_migration = "20260802_drop_legacy_valuation_snapshots"
            legacy_valuation_applied = db.execute(
                "SELECT count(*) FROM schema_migrations WHERE version = ?",
                [legacy_valuation_migration],
            ).fetchone()[0]
            if not legacy_valuation_applied:
                # opportunity_valuation_snapshots (v1) has not been written or
                # read since the v2 schema landed; its ~98K rows were pure
                # snapshot weight.
                db.execute(
                    "DROP TABLE IF EXISTS opportunity_valuation_snapshots"
                )
                db.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)",
                    [legacy_valuation_migration, utc_now()],
                )
            service_name_migration = "20260801_canonical_service_names"
            service_migration_applied = db.execute(
                "SELECT count(*) FROM schema_migrations WHERE version = ?",
                [service_name_migration],
            ).fetchone()[0]
            if not service_migration_applied:
                # Backfill-relabel historical rows to the canonical service
                # names so cross-period comparisons stop splitting one
                # service across two labels at the FOCUS cutover boundary
                # (e.g. "Storage Accounts" vs "Storage"). Pure renames only;
                # see _SERVICE_NAME_CANONICAL.
                db.execute(
                    f"""
                    UPDATE daily_cost_history
                    SET service_name = {_canonical_service_name_sql('service_name')}
                    WHERE service_name IN (
                        {', '.join('?' for _ in _SERVICE_NAME_CANONICAL)}
                    )
                    """,
                    list(_SERVICE_NAME_CANONICAL),
                )
                db.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)",
                    [service_name_migration, utc_now()],
                )
            existing = db.execute("SELECT COUNT(*) FROM azure_integration").fetchone()[0]
            if not existing:
                db.execute(
                    """
                    INSERT INTO azure_integration VALUES (
                        'azure', 'Azure', '', TRUE, ?, '[]',
                        NULL, 'never', 'Not synchronized yet.', ?
                    )
                    """,
                    [self.default_azure_provider, utc_now()],
                )

    def integration(
        self, _operational_db: Any | None = None
    ) -> dict[str, Any]:
        with self._optional_operational_connect(_operational_db) as db:
            row = db.execute(
                """
                SELECT name, tenant_id, enabled, auth_mode, subscriptions_json,
                       last_sync_at, last_sync_status, last_sync_message, updated_at
                FROM azure_integration WHERE id = 'azure'
                """
            ).fetchone()
        subscriptions = json.loads(row[4] or "[]")
        result = {
            "name": row[0],
            "tenantId": row[1] or "",
            "enabled": row[2],
            "authMode": row[3],
            "subscriptions": subscriptions,
            "lastSyncAt": row[5].isoformat() if row[5] else None,
            "lastSyncStatus": row[6],
            "lastSyncMessage": row[7],
            "updatedAt": row[8].isoformat() if row[8] else None,
        }
        result["latestSync"] = self.latest_sync(_operational_db=_operational_db)
        result["sourceFreshness"] = self.source_freshness(_operational_db=_operational_db)
        return result

    def remediation_package(
        self,
        allowlist: tuple[str, ...] = ("unattached_disk",),
        base_url: str = "https://flux.example.com",
        minimum_monthly_cost: float = 0.0,
    ) -> dict[str, Any]:
        """Build ServiceNow-ready tasks for allowlisted high-confidence
        signals, registering each new task's lifecycle so a filed
        remediation never generates a duplicate. 'exported' tasks are
        re-emitted (re-downloading a package must be idempotent);
        suppression starts once a task is reconciled as submitted or
        beyond. minimum_monthly_cost keeps sub-threshold findings out of
        the package entirely -- they are never lifecycle-registered."""
        from .remediation import SUPPRESS_STATUSES, disk_task

        findings = self.opportunities(
            source="flux_intelligence",
            confidence="High",
            include_governance=True,
            limit=500,
        )["items"]
        candidates = [
            item
            for item in findings
            if item.get("kind") in allowlist
            and item.get("lifecycleStatus", "open") == "open"
            and float(
                item.get("estimatedMonthlySavings")
                or item.get("actualMonthlyCost")
                or 0.0
            )
            >= minimum_monthly_cost
        ]
        now = utc_now()
        tasks: list[dict[str, Any]] = []
        skipped_active: list[str] = []
        with self.operational_connect() as db:
            for finding in candidates:
                resource_id = str(finding.get("resourceId") or "")
                if not resource_id:
                    continue
                virtual = {}
                try:
                    virtual = self.effective_virtual_tags(resource_id)
                except Exception:
                    virtual = {}
                task = disk_task(finding, virtual, base_url)
                existing = db.execute(
                    "SELECT status FROM remediation_tasks "
                    "WHERE correlation_key = ?",
                    [task["correlationKey"]],
                ).fetchone()
                if existing and str(existing[0]) in SUPPRESS_STATUSES:
                    skipped_active.append(task["correlationKey"])
                    continue
                db.execute(
                    """
                    INSERT INTO remediation_tasks VALUES (
                        ?, ?, ?, 'exported', '', '', ?, ?, ?
                    )
                    ON CONFLICT (correlation_key) DO UPDATE SET
                        status = 'exported',
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    [
                        task["correlationKey"], task["signalKind"],
                        resource_id.lower(), json_value(task), now, now,
                    ],
                )
                tasks.append(task)
        return {
            "generatedAt": now.isoformat(),
            "allowlist": list(allowlist),
            "minimumMonthlyCost": minimum_monthly_cost,
            "tasks": tasks,
            "skippedActiveRemediations": skipped_active,
        }

    def remediation_reconcile(
        self, updates: list[dict[str, Any]]
    ) -> dict[str, int]:
        """Map ServiceNow numbers/statuses back onto correlation keys."""
        from .remediation import ACTIVE_STATUSES, TERMINAL_STATUSES

        valid = set(ACTIVE_STATUSES) | set(TERMINAL_STATUSES)
        applied = 0
        unknown = 0
        now = utc_now()
        with self.operational_connect() as db:
            for item in updates:
                key = str(item.get("correlationKey") or "").strip()
                status = str(item.get("status") or "").strip()
                if not key or (status and status not in valid):
                    unknown += 1
                    continue
                db.execute(
                    """
                    UPDATE remediation_tasks
                    SET task_number = COALESCE(NULLIF(?, ''), task_number),
                        status = COALESCE(NULLIF(?, ''), status),
                        note = COALESCE(NULLIF(?, ''), note),
                        updated_at = ?
                    WHERE correlation_key = ?
                    """,
                    [
                        str(item.get("taskNumber") or ""),
                        status,
                        str(item.get("note") or ""),
                        now,
                        key,
                    ],
                )
                applied += 1
        return {"applied": applied, "rejected": unknown}

    def remediation_status(self) -> list[dict[str, Any]]:
        with self.operational_connect(read_only=True) as db:
            rows = db.execute(
                """
                SELECT correlation_key, signal_kind, resource_id, status,
                       task_number, note, created_at, updated_at
                FROM remediation_tasks
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [
            {
                "correlationKey": row[0],
                "signalKind": row[1],
                "resourceId": row[2],
                "status": row[3],
                "taskNumber": row[4],
                "note": row[5],
                "createdAt": row[6].isoformat() if row[6] else None,
                "updatedAt": row[7].isoformat() if row[7] else None,
            }
            for row in rows
        ]

    def virtual_tag_rules(self, include_inactive: bool = True) -> list[dict[str, Any]]:
        with self.operational_connect(read_only=True) as db:
            rows = db.execute(
                """
                SELECT rule_id, name, tag_key, tag_value, priority,
                       conditions_json, effect, status, effective_from, effective_to,
                       version, updated_by, updated_at
                FROM virtual_tag_rules
                ORDER BY priority, name
                """
            ).fetchall()
        rules = [
            {
                "ruleId": row[0],
                "name": row[1],
                "tagKey": row[2],
                "tagValue": row[3],
                "priority": int(row[4]),
                "conditions": json.loads(row[5] or "{}"),
                "effect": row[6],
                "status": row[7],
                "effectiveFrom": row[8].isoformat() if row[8] else None,
                "effectiveTo": row[9].isoformat() if row[9] else None,
                "version": int(row[10]),
                "updatedBy": row[11],
                "updatedAt": row[12].isoformat() if row[12] else None,
            }
            for row in rows
        ]
        if include_inactive:
            return rules
        return [rule for rule in rules if rule["status"] == "active"]

    def save_virtual_tag_rule(
        self, payload: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        """Create or update a rule; every change appends an audit version."""
        from .virtual_tags import validate_rule

        problems = validate_rule(payload)
        if problems:
            raise ValueError("; ".join(problems))
        rule_id = str(payload.get("ruleId") or uuid4())
        now = utc_now()
        with self.operational_connect() as db:
            existing = db.execute(
                "SELECT version FROM virtual_tag_rules WHERE rule_id = ?",
                [rule_id],
            ).fetchone()
            version = (int(existing[0]) + 1) if existing else 1
            action = "updated" if existing else "created"
            snapshot = {
                "name": payload["name"],
                "tagKey": payload["tagKey"],
                "tagValue": str(payload.get("tagValue") or ""),
                "priority": int(payload.get("priority", 100)),
                "conditions": payload.get("conditions") or {},
                "effect": str(payload.get("effect") or "include"),
                "status": str(payload.get("status") or "active"),
                "effectiveFrom": payload.get("effectiveFrom"),
                "effectiveTo": payload.get("effectiveTo"),
            }
            if existing:
                db.execute(
                    """
                    UPDATE virtual_tag_rules
                    SET name = ?, tag_key = ?, tag_value = ?, priority = ?,
                        conditions_json = ?, effect = ?, status = ?, effective_from = ?,
                        effective_to = ?, version = ?, updated_by = ?,
                        updated_at = ?
                    WHERE rule_id = ?
                    """,
                    [
                        snapshot["name"], snapshot["tagKey"],
                        snapshot["tagValue"], snapshot["priority"],
                        json_value(snapshot["conditions"]),
                        snapshot["effect"], snapshot["status"], snapshot["effectiveFrom"],
                        snapshot["effectiveTo"], version, actor, now, rule_id,
                    ],
                )
            else:
                db.execute(
                    """
                    INSERT INTO virtual_tag_rules (
                        rule_id, name, tag_key, tag_value, priority,
                        conditions_json, effect, status, effective_from,
                        effective_to, version, updated_by, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        rule_id, snapshot["name"], snapshot["tagKey"],
                        snapshot["tagValue"], snapshot["priority"],
                        json_value(snapshot["conditions"]),
                        snapshot["effect"], snapshot["status"], snapshot["effectiveFrom"],
                        snapshot["effectiveTo"], version, actor, now,
                    ],
                )
            db.execute(
                "INSERT INTO virtual_tag_rule_audit VALUES (?, ?, ?, ?, ?, ?)",
                [rule_id, version, action, json_value(snapshot), actor, now],
            )
        return {"ruleId": rule_id, "version": version, "action": action}

    def virtual_tag_dimensions(self, include_inactive: bool = True) -> list[dict[str, Any]]:
        """Return registered dimensions plus keys used by legacy data."""
        with self.operational_connect(read_only=True) as db:
            rows = db.execute(
                "SELECT dimension_key, name, description, status, version, "
                "updated_by, updated_at FROM virtual_tag_dimensions ORDER BY name"
            ).fetchall()
            legacy = db.execute(
                "SELECT DISTINCT tag_key FROM virtual_tag_rules UNION "
                "SELECT DISTINCT tag_key FROM virtual_tag_overrides"
            ).fetchall()
        dimensions = {
            str(row[0]).lower(): {
                "key": row[0], "name": row[1], "description": row[2],
                "status": row[3], "version": int(row[4]),
                "updatedBy": row[5],
                "updatedAt": row[6].isoformat() if row[6] else None,
                "implicit": False,
            }
            for row in rows
        }
        for row in legacy:
            key = str(row[0] or "").strip()
            if key and key.lower() not in dimensions:
                dimensions[key.lower()] = {
                    "key": key, "name": key, "description": "",
                    "status": "active", "version": 0, "updatedBy": "",
                    "updatedAt": None, "implicit": True,
                }
        result = sorted(dimensions.values(), key=lambda item: item["name"].lower())
        return result if include_inactive else [item for item in result if item["status"] == "active"]

    def save_virtual_tag_dimension(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        key = str(payload.get("key") or "").strip()
        name = str(payload.get("name") or key).strip()
        if not key or not re.fullmatch(r"[\w.:/@-]{1,120}", key):
            raise ValueError("key must be 1-120 tag-safe characters.")
        if not name:
            raise ValueError("name is required.")
        status = str(payload.get("status") or "active")
        if status not in ("active", "inactive"):
            raise ValueError("status must be active or inactive.")
        now = utc_now()
        with self.operational_connect() as db:
            existing = db.execute(
                "SELECT dimension_key, version FROM virtual_tag_dimensions "
                "WHERE lower(dimension_key) = ?",
                [key.lower()],
            ).fetchone()
            version = int(existing[1]) + 1 if existing else 1
            if existing:
                key = str(existing[0])
                db.execute(
                    "UPDATE virtual_tag_dimensions SET name = ?, description = ?, "
                    "status = ?, version = ?, updated_by = ?, updated_at = ? "
                    "WHERE dimension_key = ?",
                    [name, str(payload.get("description") or ""), status,
                     version, actor, now, key],
                )
            else:
                db.execute(
                    "INSERT INTO virtual_tag_dimensions (dimension_key, name, "
                    "description, status, version, updated_by, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [key, name, str(payload.get("description") or ""), status,
                     version, actor, now],
                )
        return {"key": key, "version": version, "status": status}

    def delete_virtual_tag_dimension(self, key: str, actor: str) -> None:
        now = utc_now()
        with self.operational_connect() as db:
            row = db.execute(
                "SELECT version FROM virtual_tag_dimensions WHERE lower(dimension_key) = ?",
                [key.lower()],
            ).fetchone()
            if not row:
                raise ValueError("Unknown dimension.")
            db.execute(
                "UPDATE virtual_tag_dimensions SET status = 'inactive', "
                "version = ?, updated_by = ?, updated_at = ? "
                "WHERE lower(dimension_key) = ?",
                [int(row[0]) + 1, actor, now, key.lower()],
            )

    def delete_virtual_tag_rule(self, rule_id: str, actor: str) -> None:
        self.set_virtual_tag_rule_status(rule_id, "inactive", actor)

    def set_virtual_tag_rule_status(
        self, rule_id: str, status: str, actor: str
    ) -> None:
        """Activate/deactivate keeps history; deactivation is the undo."""
        if status not in ("active", "inactive"):
            raise ValueError("status must be active or inactive")
        now = utc_now()
        with self.operational_connect() as db:
            row = db.execute(
                "SELECT version FROM virtual_tag_rules WHERE rule_id = ?",
                [rule_id],
            ).fetchone()
            if not row:
                raise ValueError("Unknown rule.")
            version = int(row[0]) + 1
            db.execute(
                "UPDATE virtual_tag_rules SET status = ?, version = ?, "
                "updated_by = ?, updated_at = ? WHERE rule_id = ?",
                [status, version, actor, now, rule_id],
            )
            db.execute(
                "INSERT INTO virtual_tag_rule_audit VALUES (?, ?, ?, ?, ?, ?)",
                [
                    rule_id, version,
                    "deactivated" if status == "inactive" else "activated",
                    json_value({"status": status}), actor, now,
                ],
            )

    def virtual_tag_overrides_for(
        self, resource_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        if not resource_ids:
            return {}
        placeholders = ", ".join("?" for _ in resource_ids)
        with self.operational_connect(read_only=True) as db:
            rows = db.execute(
                f"""
                SELECT resource_id, tag_key, tag_value, source, note,
                       updated_by, updated_at
                FROM virtual_tag_overrides
                WHERE resource_id IN ({placeholders})
                """,
                [item.lower() for item in resource_ids],
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row[0], []).append(
                {
                    "tagKey": row[1],
                    "tagValue": row[2],
                    "source": row[3],
                    "note": row[4],
                    "updatedBy": row[5],
                    "updatedAt": row[6].isoformat() if row[6] else None,
                }
            )
        return grouped

    def import_virtual_tag_overrides(
        self, overrides: list[dict[str, Any]], actor: str
    ) -> dict[str, int]:
        """Bulk upsert (imported/manual) with previous values returned so
        the caller can persist a rollback file."""
        now = utc_now()
        applied = 0
        previous: list[dict[str, Any]] = []
        with self.operational_connect() as db:
            for item in overrides:
                resource_id = str(item.get("resourceId") or "").lower()
                tag_key = str(item.get("tagKey") or "").strip()
                tag_value = str(item.get("tagValue") or "").strip()
                source = str(item.get("source") or "imported")
                if not resource_id or not tag_key or not tag_value:
                    continue
                if source not in ("imported", "manual"):
                    source = "imported"
                before = db.execute(
                    "SELECT tag_value, source FROM virtual_tag_overrides "
                    "WHERE resource_id = ? AND tag_key = ?",
                    [resource_id, tag_key],
                ).fetchone()
                previous.append(
                    {
                        "resourceId": resource_id,
                        "tagKey": tag_key,
                        "previousValue": before[0] if before else None,
                        "previousSource": before[1] if before else None,
                    }
                )
                db.execute(
                    """
                    INSERT INTO virtual_tag_overrides VALUES (
                        ?, ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT (resource_id, tag_key) DO UPDATE SET
                        tag_value = excluded.tag_value,
                        source = excluded.source,
                        note = excluded.note,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    [
                        resource_id, tag_key, tag_value, source,
                        str(item.get("note") or ""), actor, now,
                    ],
                )
                applied += 1
        return {"applied": applied, "previous": previous}

    def rollback_virtual_tag_overrides(
        self, previous: list[dict[str, Any]], actor: str
    ) -> dict[str, int]:
        """Restore imported overrides, deleting keys absent before the import.

        ``expectedValue`` is an optimistic concurrency guard supplied by the
        deploy script. A rollback never overwrites a newer production edit.
        """
        now = utc_now()
        restored = 0
        skipped = 0
        conflicts = 0
        with self.operational_connect() as db:
            for item in previous:
                resource_id = str(item.get("resourceId") or "").lower()
                tag_key = str(item.get("tagKey") or "").strip()
                if not resource_id or not tag_key:
                    skipped += 1
                    continue
                current = db.execute(
                    "SELECT tag_value FROM virtual_tag_overrides "
                    "WHERE resource_id = ? AND tag_key = ?",
                    [resource_id, tag_key],
                ).fetchone()
                expected = item.get("expectedValue")
                if expected is not None and (
                    not current or str(current[0]) != str(expected)
                ):
                    conflicts += 1
                    continue
                previous_value = item.get("previousValue")
                if previous_value is None:
                    db.execute(
                        "DELETE FROM virtual_tag_overrides "
                        "WHERE resource_id = ? AND tag_key = ?",
                        [resource_id, tag_key],
                    )
                else:
                    db.execute(
                        """
                        UPDATE virtual_tag_overrides
                        SET tag_value = ?, source = ?, note = ?,
                            updated_by = ?, updated_at = ?
                        WHERE resource_id = ? AND tag_key = ?
                        """,
                        [
                            str(previous_value),
                            str(item.get("previousSource") or "imported"),
                            "Restored by governed virtual-tag rollback",
                            actor, now, resource_id, tag_key,
                        ],
                    )
                restored += 1
        return {
            "restored": restored,
            "skipped": skipped,
            "conflicts": conflicts,
        }

    def virtual_tags_preview(
        self, payload: dict[str, Any], limit: int = 25
    ) -> dict[str, Any]:
        """Dry-run one rule against current inventory: count + sample."""
        from .virtual_tags import rule_matches, validate_rule

        problems = validate_rule(payload)
        if problems:
            raise ValueError("; ".join(problems))
        conditions = payload.get("conditions") or {}
        with self.connect(read_only=True) as db:
            rows = db.execute(
                """
                SELECT resource_id, name, subscription_id, resource_group,
                       resource_type, region, tags_json,
                       COALESCE(cost.monthly_cost, 0), cost.service_name
                FROM resources_current AS resource
                LEFT JOIN (
                    SELECT lower(resource_id) AS resource_id,
                           sum(amount) AS monthly_cost,
                           arg_max(service_name, amount) AS service_name
                    FROM costs_current WHERE cost_type = 'ActualCost'
                    GROUP BY lower(resource_id)
                ) AS cost ON cost.resource_id = lower(resource.resource_id)
                """
            ).fetchall()
        matched = []
        for row in rows:
            resource = {
                "resourceId": row[0],
                "name": row[1],
                "subscriptionId": row[2],
                "resourceGroup": row[3],
                "resourceType": row[4],
                "region": row[5],
                "tags": json.loads(row[6] or "{}"),
                "monthlyCost": float(row[7] or 0),
                "serviceName": row[8] or "",
            }
            if rule_matches(conditions, resource):
                matched.append(resource)
        return {
            "matchedCount": len(matched),
            "totalResources": len(rows),
            "matchedMonthlyCost": round(sum(item["monthlyCost"] for item in matched), 2),
            "sample": [
                {
                    "resourceId": item["resourceId"],
                    "name": item["name"],
                    "resourceGroup": item["resourceGroup"],
                    "region": item["region"],
                    "monthlyCost": round(item["monthlyCost"], 2),
                }
                for item in matched[:limit]
            ],
        }

    def effective_virtual_tags(
        self, resource_id: str
    ) -> dict[str, dict[str, str]]:
        from .virtual_tags import effective_tags

        normalized = resource_id.lower()
        with self.connect(read_only=True) as db:
            row = db.execute(
                """
                SELECT resource_id, name, subscription_id, resource_group,
                       resource_type, region, tags_json
                FROM resources_current
                WHERE lower(resource_id) = ?
                """,
                [normalized],
            ).fetchone()
        resource = {
            "resourceId": row[0] if row else resource_id,
            "name": row[1] if row else "",
            "subscriptionId": row[2] if row else "",
            "resourceGroup": row[3] if row else "",
            "resourceType": row[4] if row else "",
            "region": row[5] if row else "",
            "tags": json.loads(row[6] or "{}") if row else {},
        }
        rules = self.virtual_tag_rules(include_inactive=False)
        overrides = self.virtual_tag_overrides_for([normalized]).get(
            normalized, []
        )
        return effective_tags(
            resource, rules, overrides, utc_now().date()
        )

    def virtual_tag_report(
        self,
        *,
        dimension: str = "",
        value: str = "",
        cost_type: str = "AmortizedCost",
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict[str, Any]:
        """Cost and resource showback by effective virtual-tag value.

        Historical charges are classified using the current inventory and
        current effective rule set. Rows without a resolvable resource id are
        deliberately retained as Unclassified and the limitation is exposed
        in lineage rather than silently dropping spend.
        """
        from .virtual_tags import effective_tags

        dimensions = self.virtual_tag_dimensions(include_inactive=False)
        selected = dimension or (dimensions[0]["key"] if dimensions else "")
        rules = self.virtual_tag_rules(include_inactive=False)
        conditions = ["cost.cost_type = ?"]
        params: list[Any] = [cost_type]
        if start_date:
            conditions.append("cost.usage_date >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("cost.usage_date <= ?")
            params.append(end_date)
        with self.connect(read_only=True) as db:
            rows = db.execute(
                f"""
                SELECT CAST(date_trunc('month', cost.usage_date) AS DATE),
                       lower(cost.resource_id), cost.service_name,
                       sum(cost.amount), cost.currency,
                       resource.name, resource.subscription_id,
                       resource.subscription_name, resource.resource_group,
                       resource.resource_type, resource.region,
                       resource.tags_json
                FROM daily_cost_history AS cost
                LEFT JOIN resources_current AS resource
                  ON lower(resource.resource_id) = lower(cost.resource_id)
                WHERE {' AND '.join(conditions)}
                GROUP BY CAST(date_trunc('month', cost.usage_date) AS DATE),
                         lower(cost.resource_id), cost.service_name,
                         cost.currency, resource.name,
                         resource.subscription_id, resource.subscription_name,
                         resource.resource_group, resource.resource_type,
                         resource.region, resource.tags_json
                """,
                params,
            ).fetchall()
        resource_ids = sorted({str(row[1]) for row in rows if row[1]})
        overrides = self.virtual_tag_overrides_for(resource_ids)
        by_value: dict[str, dict[str, Any]] = {}
        monthly: dict[tuple[str, str], float] = {}
        resource_totals: dict[tuple[str, str], dict[str, Any]] = {}
        currencies: dict[str, float] = {}
        total = 0.0
        classified = 0.0
        for row in rows:
            usage_date, resource_id, service_name, amount, currency_code = row[:5]
            amount = float(amount or 0)
            total += amount
            currencies[str(currency_code or "")] = currencies.get(str(currency_code or ""), 0) + amount
            resource = {
                "resourceId": resource_id or "", "name": row[5] or "",
                "subscriptionId": row[6] or "", "subscriptionName": row[7] or "",
                "resourceGroup": row[8] or "", "resourceType": row[9] or "",
                "region": row[10] or "", "serviceName": service_name or "",
                "tags": json.loads(row[11] or "{}") if row[11] else {},
            }
            resolved = effective_tags(
                resource, rules, overrides.get(resource_id or "", []), utc_now().date()
            ) if selected else {}
            match = next(
                (item for key, item in resolved.items() if key.lower() == selected.lower()),
                None,
            )
            bucket_name = str(match.get("value")) if match else "Unclassified"
            if match:
                classified += amount
            if value and bucket_name.lower() != value.lower():
                continue
            bucket = by_value.setdefault(bucket_name, {
                "value": bucket_name, "cost": 0.0, "resourceIds": set(),
                "sources": {},
            })
            bucket["cost"] += amount
            if resource_id:
                bucket["resourceIds"].add(resource_id)
            source = str(match.get("source")) if match else "unclassified"
            bucket["sources"][source] = bucket["sources"].get(source, 0) + amount
            month = usage_date.strftime("%Y-%m") if hasattr(usage_date, "strftime") else str(usage_date)[:7]
            monthly[(month, bucket_name)] = monthly.get((month, bucket_name), 0) + amount
            if resource_id:
                key = (resource_id, bucket_name)
                item = resource_totals.setdefault(key, {
                    "resourceId": resource_id, "name": resource["name"] or resource_id,
                    "subscriptionName": resource["subscriptionName"] or resource["subscriptionId"],
                    "resourceGroup": resource["resourceGroup"],
                    "resourceType": resource["resourceType"], "value": bucket_name,
                    "source": source, "cost": 0.0,
                })
                item["cost"] += amount
        values = []
        for bucket in by_value.values():
            values.append({
                "value": bucket["value"], "cost": round(bucket["cost"], 2),
                "resourceCount": len(bucket["resourceIds"]),
                "percentOfTotal": round(bucket["cost"] / total * 100, 1) if total else None,
                "sources": bucket["sources"],
            })
        values.sort(key=lambda item: item["cost"], reverse=True)
        resources = sorted(resource_totals.values(), key=lambda item: item["cost"], reverse=True)
        for item in resources:
            item["cost"] = round(item["cost"], 2)
        currency_code = max(currencies, key=currencies.get) if currencies else "USD"
        return {
            "dimension": selected, "dimensions": dimensions, "costType": cost_type,
            "currency": currency_code, "summary": {
                "totalCost": round(total, 2), "classifiedCost": round(classified, 2),
                "classifiedPercent": round(classified / total * 100, 1) if total else None,
                "valueCount": len([item for item in values if item["value"] != "Unclassified"]),
                "resourceCount": len({item[0] for item in resource_totals}),
            },
            "values": values,
            "monthly": [
                {"month": key[0], "value": key[1], "cost": round(amount, 2)}
                for key, amount in sorted(monthly.items())
            ],
            "resources": resources, "resourcesTruncated": False,
            "lineage": {
                "costSource": "daily_cost_history", "tagEvaluation": "current effective tags",
                "precedence": "manual > imported > rule > native",
                "limitation": "Historical charges are mapped through current inventory; charges without a resolvable resource remain Unclassified.",
            },
        }

    def subscription_labels(self) -> dict[str, str]:
        """Configured friendly labels keyed by lowercase subscription ID.

        Inventory-derived names only exist after a subscription's first
        successful sync; the configured label is the fallback so raw GUIDs
        never surface in user-facing payloads for freshly added scopes.
        """
        return {
            str(item.get("subscriptionId") or "").lower(): str(
                item.get("label") or ""
            )
            for item in self.integration().get("subscriptions", [])
            if item.get("subscriptionId")
        }

    def save_integration(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.operational_connect() as db:
            db.execute(
                """
                UPDATE azure_integration
                SET name = ?, tenant_id = ?, enabled = ?, auth_mode = ?,
                    subscriptions_json = ?, updated_at = ?
                WHERE id = 'azure'
                """,
                [
                    payload["name"],
                    payload.get("tenantId", ""),
                    payload["enabled"],
                    payload["authMode"],
                    json_value(payload.get("subscriptions", [])),
                    utc_now(),
                ],
            )
        return self.integration()

    def start_sync(
        self,
        provider: str,
        trigger: str = "manual",
        sources: list[str] | tuple[str, ...] | None = None,
    ) -> str:
        sync_id = str(uuid4())
        now = utc_now()
        requested_sources = list(
            sources or ("inventory", "advisor", "intelligence", "policy", "cost")
        )
        with self.operational_connect() as db:
            db.execute(
                """
                INSERT INTO sync_runs (
                    id, provider, started_at, completed_at, status,
                    resource_count, message, trigger, stage, stage_message,
                    claimed_at, requested_sources_json
                ) VALUES (?, ?, ?, NULL, 'queued', 0, '', ?, 'queued',
                    'Synchronization queued.', NULL, ?)
                """,
                [sync_id, provider, now, trigger, json_value(requested_sources)],
            )
            db.execute(
                """
                UPDATE azure_integration
                SET last_sync_status = 'queued',
                    last_sync_message = 'Azure synchronization is queued for the durable worker.',
                    updated_at = ?
                WHERE id = 'azure'
                """,
                [now],
            )
        return sync_id

    def active_sync(self) -> dict[str, Any] | None:
        with self.operational_connect(read_only=True) as db:
            row = db.execute(
                """
                SELECT id, status
                FROM sync_runs
                WHERE status IN ('queued', 'running')
                ORDER BY started_at ASC
                LIMIT 1
                """
            ).fetchone()
        return {"id": row[0], "status": row[1]} if row else None

    @property
    def worker_id(self) -> str:
        """Stable identity for claim ownership: host and process."""
        return f"{socket.gethostname()}:{os.getpid()}"

    def claim_next_sync(self) -> dict[str, Any] | None:
        """Claim queued work, or recover work whose claim lease expired.

        Claims carry an expiring lease (``claim_expires_at``) heartbeat-
        extended by stage updates; a row left in ``running`` by a crashed
        worker becomes reclaimable when its lease lapses. Legacy rows with no
        lease recorded are treated as expired. On PostgreSQL the candidate is
        locked with FOR UPDATE SKIP LOCKED so concurrent claimants can never
        double-claim.
        """
        now = utc_now()
        with self.operational_connect() as db:
            candidate_sql = """
                SELECT id, provider, trigger, status, requested_sources_json
                FROM sync_runs
                WHERE status = 'queued'
                   OR (
                        status = 'running'
                        AND (claim_expires_at IS NULL OR claim_expires_at <= ?)
                   )
                ORDER BY
                    CASE status WHEN 'running' THEN 0 ELSE 1 END,
                    started_at ASC
                LIMIT 1
                """
            if getattr(db, "backend", "duckdb") == "postgres":
                candidate_sql += " FOR UPDATE SKIP LOCKED"
            row = db.execute(candidate_sql, [now]).fetchone()
            if not row:
                return None
            message = (
                "Recovering synchronization after a worker restart."
                if row[3] == "running"
                else "Durable worker claimed the synchronization."
            )
            db.execute(
                """
                UPDATE sync_runs
                SET status = 'running', claimed_at = ?, stage = 'starting',
                    stage_message = ?, claimed_by = ?, claim_expires_at = ?
                WHERE id = ?
                """,
                [
                    now,
                    message,
                    self.worker_id,
                    now + timedelta(seconds=_SYNC_CLAIM_LEASE_SECONDS),
                    row[0],
                ],
            )
            db.execute(
                """
                UPDATE azure_integration
                SET last_sync_status = 'running', last_sync_message = ?,
                    updated_at = ?
                WHERE id = 'azure'
                """,
                [message, now],
            )
        return {
            "id": row[0],
            "provider": row[1],
            "trigger": row[2] or "manual",
            "recovered": row[3] == "running",
            "sources": json.loads(
                row[4] or '["inventory","advisor","intelligence","policy","cost"]'
            ),
        }

    def sync_source_completed(
        self, sync_id: str, source: str, scope_id: str
    ) -> bool:
        with self.operational_connect(read_only=True) as db:
            row = db.execute(
                """
                SELECT status FROM sync_source_runs
                WHERE sync_id = ? AND source = ? AND scope_id = ?
                """,
                [sync_id, source, scope_id],
            ).fetchone()
        return bool(row and row[0] == "succeeded")

    def begin_sync_source(
        self, sync_id: str, source: str, scope_id: str
    ) -> None:
        now = utc_now()
        with self.operational_connect() as db:
            existing = db.execute(
                """
                SELECT attempt_count FROM sync_source_runs
                WHERE sync_id = ? AND source = ? AND scope_id = ?
                """,
                [sync_id, source, scope_id],
            ).fetchone()
            if existing:
                db.execute(
                    """
                    UPDATE sync_source_runs
                    SET started_at = ?, completed_at = NULL, status = 'running',
                        attempt_count = ?, row_count = 0,
                        retained_last_good = FALSE, message = ''
                    WHERE sync_id = ? AND source = ? AND scope_id = ?
                    """,
                    [now, existing[0] + 1, sync_id, source, scope_id],
                )
            else:
                db.execute(
                    """
                    INSERT INTO sync_source_runs (
                        sync_id, source, scope_id, started_at, completed_at,
                        status, attempt_count, row_count, retained_last_good,
                        message
                    ) VALUES (?, ?, ?, ?, NULL, 'running', 1, 0, FALSE, '')
                    """,
                    [sync_id, source, scope_id, now],
                )

    def record_sync_source_attempt(
        self,
        sync_id: str,
        source: str,
        scope_id: str,
        event: dict[str, Any],
    ) -> None:
        """Persist Cost Management retry guidance for operational visibility."""
        observed_at = utc_now()
        retry_after = event.get("retryAfterSeconds")
        next_retry_at = (
            observed_at + timedelta(seconds=max(0, float(retry_after)))
            if retry_after is not None
            else None
        )
        with self.operational_connect() as db:
            db.execute(
                """
                UPDATE sync_source_runs
                SET attempt_count = greatest(
                        attempt_count, ?
                    ),
                    last_attempt_at = ?,
                    status_code = COALESCE(?, status_code),
                    retry_after_seconds = COALESCE(
                        ?, retry_after_seconds
                    ),
                    qpu_consumed = COALESCE(?, qpu_consumed),
                    qpu_remaining = COALESCE(?, qpu_remaining),
                    next_retry_at = COALESCE(?, next_retry_at)
                WHERE sync_id = ? AND source = ? AND scope_id = ?
                """,
                [
                    int(event.get("attemptNumber") or 1),
                    observed_at,
                    event.get("statusCode"),
                    retry_after,
                    event.get("qpuConsumed"),
                    event.get("qpuRemaining"),
                    next_retry_at,
                    sync_id,
                    source,
                    scope_id,
                ],
            )

    def finish_sync_source(
        self,
        sync_id: str,
        source: str,
        scope_id: str,
        status: str,
        row_count: int,
        message: str,
        *,
        retained_last_good: bool = False,
    ) -> None:
        with self.operational_connect() as db:
            db.execute(
                """
                UPDATE sync_source_runs
                SET completed_at = ?, status = ?, row_count = ?,
                    retained_last_good = ?, message = ?,
                    next_retry_at = CASE
                        WHEN ? = 'succeeded' THEN NULL
                        ELSE next_retry_at
                    END
                WHERE sync_id = ? AND source = ? AND scope_id = ?
                """,
                [
                    utc_now(),
                    status,
                    row_count,
                    retained_last_good,
                    message[:1000],
                    status,
                    sync_id,
                    source,
                    scope_id,
                ],
            )

    def update_sync_stage(self, sync_id: str, stage: str, message: str) -> None:
        with self.operational_connect() as db:
            # Stage progress doubles as the claim heartbeat: a live worker
            # keeps extending its lease, and only a lapsed lease is
            # reclaimable by a replacement worker.
            db.execute(
                """
                UPDATE sync_runs
                SET stage = ?, stage_message = ?, claim_expires_at = ?
                WHERE id = ?
                """,
                [
                    stage,
                    message,
                    utc_now() + timedelta(seconds=_SYNC_CLAIM_LEASE_SECONDS),
                    sync_id,
                ],
            )
            db.execute(
                """
                UPDATE azure_integration
                SET last_sync_message = ?, updated_at = ?
                WHERE id = 'azure'
                """,
                [message, utc_now()],
            )

    def finish_sync(
        self,
        sync_id: str,
        status: str,
        message: str,
        resource_count: int = 0,
    ) -> None:
        now = utc_now()
        with self.operational_connect() as db:
            # Complete only work this worker still owns. If the claim lease
            # lapsed and another worker reclaimed the sync, the stale worker
            # must not overwrite the live run's state.
            owned = db.execute(
                """
                UPDATE sync_runs
                SET completed_at = ?, status = ?, resource_count = ?, message = ?,
                    stage = 'complete', stage_message = ?, claim_expires_at = NULL
                WHERE id = ? AND (claimed_by = ? OR claimed_by IS NULL)
                RETURNING id
                """,
                [now, status, resource_count, message, message, sync_id, self.worker_id],
            ).fetchone()
            if not owned:
                _logger.warning(
                    "Sync %s is no longer owned by %s; completion skipped.",
                    sync_id,
                    self.worker_id,
                )
                print(
                    f"Sync {sync_id} was reclaimed by another worker; "
                    "stale completion skipped."
                )
                return
            db.execute(
                """
                UPDATE azure_integration
                SET last_sync_at = CASE WHEN ? = 'succeeded' THEN ? ELSE last_sync_at END,
                    last_sync_status = ?,
                    last_sync_message = ?,
                    updated_at = ?
                WHERE id = 'azure'
                """,
                [status, now, status, message, now],
            )

    def store_snapshot(
        self,
        snapshot_id: str,
        resources: list[dict[str, Any]],
        *,
        inventory_collected: bool = True,
        costs: list[dict[str, Any]] | None = None,
        cost_scopes: list[tuple[str, str]] | None = None,
        commitment_costs: list[dict[str, Any]] | None = None,
        commitment_scopes: list[str] | None = None,
        advisor: list[dict[str, Any]] | None = None,
        advisor_collected: bool = False,
        intelligence: list[dict[str, Any]] | None = None,
        intelligence_collected: bool = False,
    ) -> None:
        observed_at = utc_now()
        resource_rows = [
            [
                snapshot_id,
                observed_at,
                item["resourceId"],
                item["name"],
                item["resourceType"],
                item["subscriptionId"],
                item.get("subscriptionName", ""),
                item.get("resourceGroup", ""),
                item.get("region", ""),
                item.get("kind", ""),
                item.get("sku", ""),
                item.get("provisioningState", ""),
                item.get("managedBy", ""),
                json_value(item.get("tags", {})),
                item.get("estimatedMonthlyCost"),
                item.get("costSource"),
                item.get("utilizationPercent"),
                item.get("utilizationSource"),
                item.get("opportunityKind"),
                item.get("opportunityReason"),
                item.get("estimatedMonthlySavings"),
                json_value(item.get("raw", {})),
            ]
            for item in resources
        ]
        cost_rows = [
            [
                snapshot_id,
                observed_at,
                item["periodStart"],
                item["periodEnd"],
                item["costType"],
                str(item["subscriptionId"]).lower(),
                str(item.get("resourceId", "")).lower(),
                item["amount"],
                item.get("currency", ""),
                item.get("source", "azure_cost_management_query"),
            ]
            for item in (costs or [])
        ]
        commitment_cost_rows = [
            [
                snapshot_id,
                observed_at,
                item["periodStart"],
                item["periodEnd"],
                str(item["subscriptionId"]).lower(),
                str(item.get("meterId", "")).lower(),
                item.get("pricingModel", "Unknown"),
                item["amount"],
                item.get("currency", ""),
                item.get("source", "azure_cost_management_query"),
            ]
            for item in (commitment_costs or [])
        ]
        advisor_rows = [
            [
                snapshot_id,
                observed_at,
                item["recommendationId"],
                str(item.get("resourceId", "")).lower(),
                str(item.get("subscriptionId", "")).lower(),
                item.get("subscriptionName", ""),
                item.get("resourceType", ""),
                item.get("category", ""),
                item.get("impact", ""),
                item.get("problem", ""),
                item.get("solution", ""),
                item.get("savingsAmount"),
                item.get("savingsCurrency", ""),
                json_value(item.get("raw", {})),
                item.get("annualSavingsAmount"),
                item.get("recommendationTypeId", ""),
                item.get("currentSku", ""),
                item.get("recommendedSku", ""),
                item.get("lastUpdated") or None,
                item.get("learnMoreLink", ""),
            ]
            for item in (advisor or [])
        ]
        intelligence_rows = [
            [
                snapshot_id,
                observed_at,
                item["findingId"],
                item["ruleId"],
                item.get("source", "flux_intelligence"),
                str(item.get("resourceId", "")).lower(),
                str(item.get("relatedResourceId", "")).lower(),
                str(item.get("subscriptionId", "")).lower(),
                item.get("subscriptionName", ""),
                item.get("resourceType", ""),
                item.get("resourceGroup", ""),
                item.get("region", ""),
                item.get("category", ""),
                item.get("impact", ""),
                item.get("confidence", ""),
                item.get("title", ""),
                item.get("reason", ""),
                json_value(item.get("evidence", {})),
                item.get("estimatedMonthlySavings"),
                item.get("savingsCurrency", ""),
                item.get("ruleVersion", ""),
            ]
            for item in (intelligence or [])
        ]
        with self.connect() as db:
            db.execute("BEGIN TRANSACTION")
            try:
                if resource_rows:
                    db.executemany(
                        """
                        INSERT INTO resource_snapshots VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        resource_rows,
                    )
                if inventory_collected:
                    db.execute(
                        "INSERT INTO source_sync_state VALUES (?, ?, ?, ?, ?)",
                        [
                            snapshot_id,
                            observed_at,
                            "AzureResourceGraph",
                            "configured-subscriptions",
                            len(resource_rows),
                        ],
                    )
                if cost_rows:
                    db.executemany(
                        """
                        INSERT INTO cost_snapshots VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        cost_rows,
                    )
                for subscription_id, cost_type in cost_scopes or []:
                    row_count = sum(
                        1
                        for item in costs or []
                        if str(item["subscriptionId"]).lower() == subscription_id.lower()
                        and item["costType"] == cost_type
                    )
                    db.execute(
                        "INSERT INTO source_sync_state VALUES (?, ?, ?, ?, ?)",
                        [
                            snapshot_id,
                            observed_at,
                            cost_type,
                            subscription_id,
                            row_count,
                        ],
                    )
                if commitment_cost_rows:
                    db.executemany(
                        """
                        INSERT INTO commitment_cost_snapshots (
                            snapshot_id, observed_at, period_start, period_end,
                            subscription_id, meter_id, pricing_model, amount,
                            currency, source
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        commitment_cost_rows,
                    )
                for subscription_id in commitment_scopes or []:
                    row_count = sum(
                        1
                        for item in commitment_costs or []
                        if str(item["subscriptionId"]).lower()
                        == subscription_id.lower()
                    )
                    db.execute(
                        "INSERT INTO source_sync_state VALUES (?, ?, ?, ?, ?)",
                        [
                            snapshot_id,
                            observed_at,
                            "CommitmentCoverage",
                            subscription_id.lower(),
                            row_count,
                        ],
                    )
                if advisor_rows:
                    db.executemany(
                        """
                        INSERT INTO advisor_recommendation_snapshots (
                            snapshot_id, observed_at, recommendation_id,
                            resource_id, subscription_id, subscription_name,
                            resource_type, category, impact, problem, solution,
                            savings_amount, savings_currency, raw_json,
                            annual_savings_amount, recommendation_type_id,
                            current_sku, recommended_sku, last_updated,
                            learn_more_link
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        advisor_rows,
                    )
                if advisor_collected:
                    db.execute(
                        "INSERT INTO source_sync_state VALUES (?, ?, ?, ?, ?)",
                        [
                            snapshot_id,
                            observed_at,
                            "AzureAdvisor",
                            "configured-subscriptions",
                            len(advisor_rows),
                        ],
                    )
                if intelligence_rows:
                    db.executemany(
                        """
                        INSERT INTO rule_opportunity_snapshots VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        intelligence_rows,
                    )
                if intelligence_collected:
                    db.execute(
                        "INSERT INTO source_sync_state VALUES (?, ?, ?, ?, ?)",
                        [
                            snapshot_id,
                            observed_at,
                            "FluxIntelligence",
                            "configured-subscriptions",
                            len(intelligence_rows),
                        ],
                    )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        self._refresh_after_snapshot(
            inventory=inventory_collected,
            costs=bool(cost_rows),
            commitment=bool(commitment_cost_rows),
            advisor=advisor_collected,
            rules=intelligence_collected,
        )

    def _refresh_after_snapshot(
        self,
        *,
        inventory: bool = False,
        costs: bool = False,
        commitment: bool = False,
        advisor: bool = False,
        rules: bool = False,
    ) -> None:
        """Rebuild only the materialized tables affected by a snapshot write."""
        if not (inventory or costs or commitment or advisor or rules):
            return
        with self.connect() as db:
            if inventory:
                db.execute(
                    "CREATE OR REPLACE TABLE resources_current AS "
                    + self._MATERIALIZED_TABLES["resources_current"].strip()
                )
            if costs:
                db.execute(
                    "CREATE OR REPLACE TABLE costs_current AS "
                    + self._MATERIALIZED_TABLES["costs_current"].strip()
                )
            if commitment:
                db.execute(
                    "CREATE OR REPLACE TABLE commitment_costs_current AS "
                    + self._MATERIALIZED_TABLES["commitment_costs_current"].strip()
                )
            if advisor:
                db.execute(
                    "CREATE OR REPLACE TABLE advisor_recommendations_current AS "
                    + self._MATERIALIZED_TABLES[
                        "advisor_recommendations_current"
                    ].strip()
                )
            if rules:
                db.execute(
                    "CREATE OR REPLACE TABLE rule_opportunities_current AS "
                    + self._MATERIALIZED_TABLES[
                        "rule_opportunities_current"
                    ].strip()
                )

    def _refresh_opportunity_scores(self, kind: str) -> None:
        """Rebuild the materialized confidence or valuation table.

        These derive from compute_opportunity_confidence and
        compute_opportunity_valuation rather than from a snapshot write, so
        each recompute must republish its materialized result.
        """
        name = (
            "opportunity_confidence_current"
            if kind == "confidence"
            else "opportunity_valuation_current"
        )
        with self.connect() as db:
            db.execute(
                f"CREATE OR REPLACE TABLE {name} AS "
                + self._MATERIALIZED_TABLES[name].strip()
            )

    def store_policy_posture(
        self,
        snapshot_id: str,
        rows: list[dict[str, Any]],
        resources: list[dict[str, Any]] | None = None,
    ) -> int:
        observed_at = utc_now()
        values = [
            [
                snapshot_id,
                observed_at,
                str(item.get("subscriptionId") or "").lower(),
                item.get("subscriptionName", ""),
                str(item.get("assignmentId") or "").lower(),
                item.get("assignmentName", ""),
                int(item.get("evaluatedCount") or 0),
                int(item.get("compliantCount") or 0),
                int(item.get("nonCompliantCount") or 0),
                int(item.get("exemptCount") or 0),
                int(item.get("unknownCount") or 0),
                int(item.get("resourceCount") or 0),
                int(item.get("definitionCount") or 0),
            ]
            for item in rows
        ]
        resource_values = [
            [
                snapshot_id,
                observed_at,
                str(item.get("subscriptionId") or "").lower(),
                item.get("subscriptionName", ""),
                str(item.get("assignmentId") or "").lower(),
                item.get("assignmentName", ""),
                str(item.get("definitionId") or "").lower(),
                item.get("definitionName", ""),
                item.get("complianceState", "Unknown"),
                str(item.get("resourceId") or "").lower(),
                item.get("resourceName", ""),
                str(item.get("resourceType") or "").lower(),
                item.get("region", ""),
                str(item.get("exemptionId") or "").lower(),
                item.get("evaluatedAt", ""),
            ]
            for item in (resources or [])
        ]
        with self.connect() as db:
            db.execute("BEGIN TRANSACTION")
            try:
                if values:
                    db.executemany(
                        """
                        INSERT INTO policy_posture_snapshots VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        values,
                    )
                if resource_values:
                    db.executemany(
                        """
                        INSERT INTO policy_resource_snapshots VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        resource_values,
                    )
                db.execute(
                    "INSERT INTO source_sync_state VALUES (?, ?, ?, ?, ?)",
                    [
                        snapshot_id,
                        observed_at,
                        "AzurePolicy",
                        "configured-subscriptions",
                        len(values),
                    ],
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        with self.connect() as db:
            db.execute(
                "CREATE OR REPLACE TABLE policy_posture_current AS "
                + self._MATERIALIZED_TABLES["policy_posture_current"].strip()
            )
        return len(values)

    def daily_cost_query_start(
        self,
        subscription_id: str,
        cost_type: str,
        *,
        initial_days: int,
        refresh_days: int,
        as_of: date | None = None,
    ) -> date:
        today = as_of or utc_now().date()
        with self.operational_connect(read_only=True) as db:
            completed_backfill = db.execute(
                """
                SELECT count(*)
                FROM cost_history_scope_runs
                WHERE subscription_id = ? AND cost_type = ?
                  AND status = 'succeeded'
                  AND (query_end - query_start) + 1 >= ?
                  AND message NOT ILIKE
                      'Query API was unavailable;%'
                """,
                [
                    subscription_id.lower(),
                    cost_type,
                    max(initial_days, 1),
                ],
            ).fetchone()[0]
        # A partially committed chunk is valuable retained data, but it must
        # not make an interrupted first backfill look complete. Only a
        # successful full-width scope advances to the rolling refresh window.
        days = refresh_days if completed_backfill else initial_days
        return today - timedelta(days=max(days, 1) - 1)

    def start_cost_history_run(
        self,
        run_id: str,
        expected_scopes: int,
    ) -> None:
        with self.operational_connect() as db:
            db.execute(
                """
                INSERT INTO cost_history_runs VALUES (
                    ?, ?, NULL, 'running', ?, 0, 0, 0, ''
                )
                """,
                [run_id, utc_now(), expected_scopes],
            )

    def begin_cost_history_scope(
        self,
        run_id: str,
        subscription_id: str,
        cost_type: str,
        query_start: date,
        query_end: date,
    ) -> None:
        with self.operational_connect() as db:
            db.execute(
                """
                INSERT INTO cost_history_scope_runs VALUES (
                    ?, ?, ?, ?, NULL, 'running', ?, ?, 0, FALSE, NULL, ''
                )
                """,
                [
                    run_id,
                    subscription_id.lower(),
                    cost_type,
                    utc_now(),
                    query_start,
                    query_end,
                ],
            )

    def finish_cost_history_scope(
        self,
        run_id: str,
        subscription_id: str,
        cost_type: str,
        *,
        status: str,
        row_count: int = 0,
        retained_last_good: bool = False,
        status_code: int | None = None,
        message: str = "",
    ) -> None:
        with self.operational_connect() as db:
            db.execute(
                """
                UPDATE cost_history_scope_runs
                SET completed_at = ?, status = ?, row_count = ?,
                    retained_last_good = ?, status_code = ?, message = ?
                WHERE run_id = ? AND subscription_id = ? AND cost_type = ?
                """,
                [
                    utc_now(),
                    status,
                    row_count,
                    retained_last_good,
                    status_code,
                    message[:1000],
                    run_id,
                    subscription_id.lower(),
                    cost_type,
                ],
            )

    def record_cost_history_request_attempt(
        self,
        run_id: str,
        subscription_id: str,
        cost_type: str,
        event: dict[str, Any],
    ) -> None:
        with self.operational_connect() as db:
            db.execute(
                """
                INSERT INTO cost_history_request_attempts (
                    attempt_id, run_id, subscription_id, cost_type,
                    observed_at, attempt_number, status, status_code,
                    retry_after_seconds, qpu_consumed, qpu_remaining, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    str(uuid4()),
                    run_id,
                    subscription_id.lower(),
                    cost_type,
                    utc_now(),
                    int(event.get("attemptNumber") or 1),
                    str(event.get("status") or "unknown"),
                    event.get("statusCode"),
                    event.get("retryAfterSeconds"),
                    event.get("qpuConsumed"),
                    event.get("qpuRemaining"),
                    str(event.get("message") or "")[:1000],
                ],
            )

    def finish_cost_history_run(
        self,
        run_id: str,
        *,
        status: str,
        completed_scopes: int,
        failed_scopes: int,
        row_count: int,
        message: str,
    ) -> None:
        with self.operational_connect() as db:
            db.execute(
                """
                UPDATE cost_history_runs
                SET completed_at = ?, status = ?, completed_scopes = ?,
                    failed_scopes = ?, row_count = ?, message = ?
                WHERE run_id = ?
                """,
                [
                    utc_now(),
                    status,
                    completed_scopes,
                    failed_scopes,
                    row_count,
                    message[:2000],
                    run_id,
                ],
            )

    def cost_history_scope_order(
        self,
        scopes: list[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        """Put never-completed and previously failed scopes first."""
        if not scopes:
            return []
        with self.operational_connect(read_only=True) as db:
            rows = db.execute(
                """
                WITH ranked AS (
                    SELECT subscription_id, cost_type, status, completed_at,
                        row_number() OVER (
                            PARTITION BY subscription_id, cost_type
                            ORDER BY started_at DESC, run_id DESC
                        ) AS rank
                    FROM cost_history_scope_runs
                )
                SELECT subscription_id, cost_type, status
                FROM ranked WHERE rank = 1
                """
            ).fetchall()
        with self.connect(read_only=True) as db:
            existing_rows = db.execute(
                """
                SELECT DISTINCT subscription_id, cost_type
                FROM daily_cost_history
                """
            ).fetchall()
        last_status = {(row[0], row[1]): row[2] for row in rows}
        existing = {(row[0], row[1]) for row in existing_rows}
        priority = {
            "failed": 0,
            "running": 1,
            "succeeded": 3,
        }
        return sorted(
            scopes,
            key=lambda scope: (
                priority.get(
                    last_status.get(scope, ""),
                    2 if scope in existing else 0,
                ),
                scope[0],
                scope[1],
            ),
        )

    def next_cost_details_backfill_period(
        self,
        subscription_id: str,
        cost_type: str,
        *,
        initial_days: int,
        current_refresh_days: int,
        as_of: date | None = None,
    ) -> tuple[date, date] | None:
        """Return the next bounded calendar-month report for a failed scope."""
        today = as_of or utc_now().date()
        earliest = today - timedelta(days=max(initial_days, 1) - 1)
        month = today.replace(day=1)
        earliest_month = earliest.replace(day=1)
        periods: list[tuple[date, date]] = []
        while month >= earliest_month:
            if month.year == today.year and month.month == today.month:
                period_end = today
            else:
                next_month = (
                    month.replace(year=month.year + 1, month=1)
                    if month.month == 12
                    else month.replace(month=month.month + 1)
                )
                period_end = next_month - timedelta(days=1)
            periods.append((month, period_end))
            month = (
                month.replace(year=month.year - 1, month=12)
                if month.month == 1
                else month.replace(month=month.month - 1)
            )

        normalized_subscription = subscription_id.lower()
        with self.operational_connect(read_only=True) as db:
            rows = db.execute(
                """
                SELECT period_start, period_end, status, last_attempt_at,
                       next_retry_at
                FROM cost_details_backfill_scopes
                WHERE subscription_id = ? AND cost_type = ?
                """,
                [normalized_subscription, cost_type],
            ).fetchall()
        checkpoints = {row[0]: row for row in rows}
        now = utc_now()
        for period_start, period_end in periods:
            checkpoint = checkpoints.get(period_start)
            if checkpoint is None:
                return period_start, period_end
            status = checkpoint[2]
            last_attempt_at = checkpoint[3]
            next_retry_at = checkpoint[4]
            if status == "unsupported":
                continue
            if status == "succeeded":
                is_current = period_start == today.replace(day=1)
                refresh_due = (
                    is_current
                    and last_attempt_at is not None
                    and last_attempt_at
                    <= now - timedelta(days=max(current_refresh_days, 1))
                )
                if refresh_due:
                    return period_start, period_end
                continue
            if status == "running" and last_attempt_at:
                if last_attempt_at > now - timedelta(hours=6):
                    continue
            if next_retry_at is None or next_retry_at <= now:
                return period_start, period_end
        return None

    def begin_cost_details_backfill(
        self,
        subscription_id: str,
        cost_type: str,
        period_start: date,
        period_end: date,
    ) -> None:
        now = utc_now()
        with self.operational_connect() as db:
            db.execute(
                """
                INSERT INTO cost_details_backfill_scopes VALUES (
                    ?, ?, ?, ?, 'running', 1, 0, ?, ?, NULL, NULL,
                    NULL, '', 'azure_cost_details_report'
                )
                ON CONFLICT (subscription_id, cost_type, period_start)
                DO UPDATE SET
                    period_end = excluded.period_end,
                    status = 'running',
                    attempt_count =
                        cost_details_backfill_scopes.attempt_count + 1,
                    last_attempt_at = excluded.last_attempt_at,
                    completed_at = NULL,
                    next_retry_at = NULL,
                    status_code = NULL,
                    message = ''
                """,
                [
                    subscription_id.lower(),
                    cost_type,
                    period_start,
                    period_end,
                    now,
                    now,
                ],
            )

    def finish_cost_details_backfill(
        self,
        subscription_id: str,
        cost_type: str,
        period_start: date,
        *,
        status: str,
        row_count: int = 0,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        message: str = "",
    ) -> None:
        next_retry_at = (
            utc_now() + timedelta(seconds=max(retry_after_seconds, 0))
            if retry_after_seconds is not None
            else None
        )
        with self.operational_connect() as db:
            db.execute(
                """
                UPDATE cost_details_backfill_scopes
                SET status = ?, row_count = ?, completed_at = ?,
                    next_retry_at = ?, status_code = ?, message = ?
                WHERE subscription_id = ? AND cost_type = ?
                  AND period_start = ?
                """,
                [
                    status,
                    row_count,
                    utc_now(),
                    next_retry_at,
                    status_code,
                    message[:1000],
                    subscription_id.lower(),
                    cost_type,
                    period_start,
                ],
            )

    def cost_sync_scope_order(
        self,
        scopes: list[tuple[str, str, str]],
    ) -> list[tuple[str, str, str]]:
        """Prioritize missing and failed current-cost scopes.

        Commitment collection is intentionally first within each priority tier.
        It used to run after every actual and amortized query, which meant a
        tenant throttle could repeatedly prevent a never-collected commitment
        scope from receiving its first attempt.
        """
        if not scopes:
            return []
        with self.connect(read_only=True) as db:
            current_rows = db.execute(
                """
                SELECT source, scope_id, max(observed_at)
                FROM source_sync_state
                WHERE source IN (
                    'ActualCost', 'AmortizedCost', 'CommitmentCoverage'
                )
                GROUP BY source, scope_id
                """
            ).fetchall()
        with self.operational_connect(read_only=True) as db:
            attempt_rows = db.execute(
                """
                WITH ranked AS (
                    SELECT source, scope_id, status, started_at,
                        row_number() OVER (
                            PARTITION BY source, scope_id
                            ORDER BY started_at DESC, sync_id DESC
                        ) AS rank
                    FROM sync_source_runs
                    WHERE source IN (
                        'ActualCost', 'AmortizedCost', 'CommitmentCoverage'
                    )
                )
                SELECT source, scope_id, status
                FROM ranked WHERE rank = 1
                """
            ).fetchall()
        last_status = {
            (str(row[0]), str(row[1])): str(row[2])
            for row in attempt_rows
        }
        current = {
            (str(row[0]), str(row[1])): row[2]
            for row in current_rows
        }
        source_priority = {
            "CommitmentCoverage": 0,
            "ActualCost": 1,
            "AmortizedCost": 2,
        }

        def priority(scope: tuple[str, str, str]) -> tuple[Any, ...]:
            subscription_id, _label, source = scope
            key = (source, subscription_id)
            status = last_status.get(key, "")
            if status == "failed" or key not in current:
                state_priority = 0
            elif status == "running":
                state_priority = 1
            else:
                state_priority = 2
            observed_at = current.get(key)
            return (
                state_priority,
                source_priority.get(source, 9),
                observed_at.isoformat() if observed_at else "",
                subscription_id,
            )

        return sorted(scopes, key=priority)

    def cost_history_status(self) -> dict[str, Any]:
        integration = self.integration()
        names = {
            item["subscriptionId"].lower(): (
                item.get("label") or item["subscriptionId"]
            )
            for item in integration.get("subscriptions", [])
            if item.get("subscriptionId")
        }
        with self.operational_connect(read_only=True) as db:
            run = db.execute(
                """
                SELECT run_id, started_at, completed_at, status,
                       expected_scopes, completed_scopes, failed_scopes,
                       row_count, message
                FROM cost_history_runs
                ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
            scopes = db.execute(
                """
                WITH ranked AS (
                    SELECT *,
                        row_number() OVER (
                            PARTITION BY subscription_id, cost_type
                            ORDER BY started_at DESC, run_id DESC
                        ) AS rank
                    FROM cost_history_scope_runs
                )
                SELECT run_id, subscription_id, cost_type, started_at,
                       completed_at, status, query_start, query_end,
                       row_count, retained_last_good, status_code, message
                FROM ranked
                WHERE rank = 1
                ORDER BY
                    CASE status WHEN 'failed' THEN 1
                        WHEN 'running' THEN 2 ELSE 3 END,
                    subscription_id, cost_type
                """
            ).fetchall()
            request_attempt_rows = db.execute(
                """
                SELECT run_id, subscription_id, cost_type, observed_at,
                       status, retry_after_seconds, qpu_consumed, qpu_remaining
                FROM cost_history_request_attempts
                ORDER BY observed_at ASC
                """
            ).fetchall()
            quota_rows = db.execute(
                """
                SELECT name, next_allowed_at, cooldown_until, updated_at
                FROM cost_management_quota_state
                ORDER BY name
                """
            ).fetchall()
            backfill_rows = db.execute(
                """
                SELECT subscription_id, cost_type, period_start, period_end,
                       status, attempt_count, row_count, first_attempt_at,
                       last_attempt_at, completed_at, next_retry_at,
                       status_code, message, source
                FROM cost_details_backfill_scopes
                ORDER BY period_start DESC, subscription_id, cost_type
                """
            ).fetchall()
        attempt_status: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in request_attempt_rows:
            key = (row[0], row[1], row[2])
            observed_at = row[3]
            retry_after = row[5]
            status = attempt_status.setdefault(
                key,
                {
                    "attemptCount": 0,
                    "retryCount": 0,
                    "lastAttemptAt": None,
                    "nextRetryAt": None,
                    "retryAfterSeconds": None,
                    "qpuConsumed": None,
                    "qpuRemaining": None,
                },
            )
            status["attemptCount"] += 1
            if row[4] == "retrying":
                status["retryCount"] += 1
            status["lastAttemptAt"] = (
                observed_at.isoformat() if observed_at else None
            )
            if row[6] is not None:
                status["qpuConsumed"] = row[6]
            if row[7] is not None:
                status["qpuRemaining"] = row[7]
            if retry_after is not None and observed_at is not None:
                status["retryAfterSeconds"] = retry_after
                status["nextRetryAt"] = (
                    observed_at + timedelta(seconds=float(retry_after))
                ).isoformat()
        return {
            "latestRun": {
                "runId": run[0],
                "startedAt": run[1].isoformat(),
                "completedAt": run[2].isoformat() if run[2] else None,
                "status": run[3],
                "expectedScopes": run[4],
                "completedScopes": run[5],
                "failedScopes": run[6],
                "rowCount": run[7],
                "message": run[8],
            }
            if run
            else None,
            "scopes": [
                {
                    "runId": row[0],
                    "subscriptionId": row[1],
                    "subscriptionName": names.get(row[1], row[1]),
                    "costType": row[2],
                    "startedAt": row[3].isoformat(),
                    "completedAt": (
                        row[4].isoformat() if row[4] else None
                    ),
                    "status": row[5],
                    "queryStart": row[6].isoformat(),
                    "queryEnd": row[7].isoformat(),
                    "rowCount": row[8],
                    "retainedLastGood": row[9],
                    "statusCode": row[10],
                    "message": row[11],
                    **attempt_status.get(
                        (row[0], row[1], row[2]),
                        {
                            "attemptCount": 0,
                            "retryCount": 0,
                            "lastAttemptAt": None,
                            "nextRetryAt": None,
                            "retryAfterSeconds": None,
                            "qpuConsumed": None,
                            "qpuRemaining": None,
                        },
                    ),
                }
                for row in scopes
            ],
            "backfill": {
                "completedPeriods": sum(
                    1 for row in backfill_rows if row[4] == "succeeded"
                ),
                "failedPeriods": sum(
                    1 for row in backfill_rows if row[4] == "failed"
                ),
                "runningPeriods": sum(
                    1 for row in backfill_rows if row[4] == "running"
                ),
                "periods": [
                    {
                        "subscriptionId": row[0],
                        "subscriptionName": names.get(row[0], row[0]),
                        "costType": row[1],
                        "periodStart": row[2].isoformat(),
                        "periodEnd": row[3].isoformat(),
                        "status": row[4],
                        "attemptCount": row[5],
                        "rowCount": row[6],
                        "firstAttemptAt": (
                            row[7].isoformat() if row[7] else None
                        ),
                        "lastAttemptAt": (
                            row[8].isoformat() if row[8] else None
                        ),
                        "completedAt": (
                            row[9].isoformat() if row[9] else None
                        ),
                        "nextRetryAt": (
                            row[10].isoformat() if row[10] else None
                        ),
                        "statusCode": row[11],
                        "message": row[12],
                        "source": row[13],
                    }
                    for row in backfill_rows
                ],
            },
            "quota": [
                {
                    "name": row[0],
                    "nextAllowedAt": row[1].isoformat() if row[1] else None,
                    "cooldownUntil": row[2].isoformat() if row[2] else None,
                    "updatedAt": row[3].isoformat() if row[3] else None,
                }
                for row in quota_rows
            ],
        }

    def cost_history_coverage(
        self,
        *,
        initial_days: int,
        as_of: date | None = None,
        max_ranges_per_scope: int = 12,
    ) -> dict[str, Any]:
        """Day-level completeness ledger for every configured cost scope.

        Run status says whether the latest attempt worked; this says whether
        the data is actually there. Expected days run from the collection
        window floor to the finalized horizon (Azure finalizes 24-48h late),
        and every configured subscription x cost type pair is accounted for
        even when it has ingested nothing at all -- absence is the loudest
        signal. Anomaly baselines treat missing days as zero spend, so holes
        here silently distort every consumer downstream.
        """
        today = as_of or utc_now().date()
        finalized_end = today - timedelta(days=2)
        window_start = today - timedelta(days=max(initial_days, 1) - 1)
        integration = self.integration()
        subscriptions = [
            {
                "id": item["subscriptionId"].lower(),
                "name": item.get("label") or item["subscriptionId"],
            }
            for item in integration.get("subscriptions", [])
            if item.get("subscriptionId")
        ]
        expected_days = max((finalized_end - window_start).days + 1, 0)
        ingested: dict[tuple[str, str], set[date]] = {}
        first_seen: dict[tuple[str, str], date] = {}
        if subscriptions and expected_days:
            with self.connect(read_only=True) as db:
                rows = db.execute(
                    """
                    SELECT subscription_id, cost_type,
                           usage_date, min(usage_date) OVER (
                               PARTITION BY subscription_id, cost_type
                           )
                    FROM daily_cost_history
                    WHERE usage_date BETWEEN ? AND ?
                    GROUP BY subscription_id, cost_type, usage_date
                    """,
                    [window_start, finalized_end],
                ).fetchall()
            for row in rows:
                key = (str(row[0]).lower(), str(row[1]))
                ingested.setdefault(key, set()).add(row[2])
                first_seen[key] = row[3]
        scopes: list[dict[str, Any]] = []
        estate_expected = estate_ingested = complete_scopes = 0
        for subscription in subscriptions:
            for cost_type in ("ActualCost", "AmortizedCost"):
                key = (subscription["id"], cost_type)
                days = ingested.get(key, set())
                missing_ranges: list[dict[str, str]] = []
                run_start: date | None = None
                previous_missing = False
                cursor = window_start
                missing_count = 0
                while cursor <= finalized_end:
                    is_missing = cursor not in days
                    if is_missing:
                        missing_count += 1
                        if not previous_missing:
                            run_start = cursor
                    elif previous_missing and run_start is not None:
                        missing_ranges.append(
                            {
                                "start": run_start.isoformat(),
                                "end": (
                                    cursor - timedelta(days=1)
                                ).isoformat(),
                            }
                        )
                    previous_missing = is_missing
                    cursor += timedelta(days=1)
                if previous_missing and run_start is not None:
                    missing_ranges.append(
                        {
                            "start": run_start.isoformat(),
                            "end": finalized_end.isoformat(),
                        }
                    )
                ingested_count = expected_days - missing_count
                estate_expected += expected_days
                estate_ingested += ingested_count
                if missing_count == 0:
                    complete_scopes += 1
                scopes.append(
                    {
                        "subscriptionId": subscription["id"],
                        "subscriptionName": subscription["name"],
                        "costType": cost_type,
                        "windowStart": window_start.isoformat(),
                        "windowEnd": finalized_end.isoformat(),
                        "expectedDays": expected_days,
                        "ingestedDays": ingested_count,
                        "missingDays": missing_count,
                        "coveragePercent": (
                            round(ingested_count / expected_days * 100, 1)
                            if expected_days
                            else None
                        ),
                        "firstIngestedDay": (
                            first_seen[key].isoformat()
                            if key in first_seen
                            else None
                        ),
                        "missingRanges": missing_ranges[:max_ranges_per_scope],
                        "missingRangesTruncated": (
                            len(missing_ranges) > max_ranges_per_scope
                        ),
                    }
                )
        scopes.sort(key=lambda scope: (scope["coveragePercent"] or 0))
        return {
            "windowStart": window_start.isoformat(),
            "windowEnd": finalized_end.isoformat(),
            "scopeCount": len(scopes),
            "completeScopes": complete_scopes,
            "expectedScopeDays": estate_expected,
            "ingestedScopeDays": estate_ingested,
            "coveragePercent": (
                round(estate_ingested / estate_expected * 100, 1)
                if estate_expected
                else None
            ),
            "scopes": scopes,
        }

    def requeue_cost_coverage_gaps(
        self,
        *,
        initial_days: int,
        as_of: date | None = None,
        limit: int = 3,
        cooldown_days: int = 7,
    ) -> dict[str, Any]:
        """Turn ledger gaps into eligible Cost Details backfill months.

        A month whose checkpoint says ``succeeded`` is never revisited by
        the fallback, so a partial ingestion behind a green checkpoint stays
        a hole forever. This flips such months to ``gap`` (retry-eligible,
        oldest first, bounded by ``limit``) and returns them so the sync job
        can feed them through the existing throttle-aware fallback path. The
        cooldown prevents hammering months Azure genuinely has no data for
        (a young subscription's pre-creation window re-fetches at most once
        per cooldown).
        """
        coverage = self.cost_history_coverage(
            initial_days=initial_days, as_of=as_of
        )
        candidates: dict[tuple[str, str, date], dict[str, Any]] = {}
        for scope in coverage["scopes"]:
            for missing in scope["missingRanges"]:
                cursor = date.fromisoformat(missing["start"]).replace(day=1)
                range_end = date.fromisoformat(missing["end"])
                while cursor <= range_end:
                    key = (
                        scope["subscriptionId"],
                        scope["costType"],
                        cursor,
                    )
                    entry = candidates.setdefault(
                        key,
                        {
                            "subscriptionId": scope["subscriptionId"],
                            "costType": scope["costType"],
                            "periodStart": cursor,
                            "missingDays": 0,
                        },
                    )
                    next_month = (
                        cursor.replace(year=cursor.year + 1, month=1)
                        if cursor.month == 12
                        else cursor.replace(month=cursor.month + 1)
                    )
                    overlap_start = max(
                        cursor, date.fromisoformat(missing["start"])
                    )
                    overlap_end = min(
                        next_month - timedelta(days=1), range_end
                    )
                    entry["missingDays"] += (
                        (overlap_end - overlap_start).days + 1
                    )
                    cursor = next_month
        if not candidates:
            return {"requeued": [], "candidateMonths": 0}
        now = utc_now()
        today = as_of or utc_now().date()
        requeued: list[dict[str, Any]] = []
        with self.operational_connect() as db:
            for key in sorted(candidates):
                if len(requeued) >= max(limit, 0):
                    break
                subscription_id, cost_type, period_start = key
                row = db.execute(
                    """
                    SELECT status, last_attempt_at, next_retry_at
                    FROM cost_details_backfill_scopes
                    WHERE subscription_id = ? AND cost_type = ?
                      AND period_start = ?
                    """,
                    [subscription_id, cost_type, period_start],
                ).fetchone()
                if row is not None:
                    status, last_attempt_at, next_retry_at = row
                    if status == "unsupported":
                        continue
                    if status == "running":
                        continue
                    if next_retry_at is not None and next_retry_at > now:
                        continue
                    if (
                        last_attempt_at is not None
                        and last_attempt_at
                        > now - timedelta(days=max(cooldown_days, 1))
                    ):
                        continue
                    if status == "succeeded":
                        db.execute(
                            """
                            UPDATE cost_details_backfill_scopes
                            SET status = 'gap', next_retry_at = NULL,
                                message = ?
                            WHERE subscription_id = ? AND cost_type = ?
                              AND period_start = ?
                            """,
                            [
                                "Coverage ledger found "
                                f"{candidates[key]['missingDays']} missing "
                                "days behind a succeeded checkpoint.",
                                subscription_id,
                                cost_type,
                                period_start,
                            ],
                        )
                next_month = (
                    period_start.replace(year=period_start.year + 1, month=1)
                    if period_start.month == 12
                    else period_start.replace(month=period_start.month + 1)
                )
                period_end = min(next_month - timedelta(days=1), today)
                requeued.append(
                    {
                        "subscriptionId": subscription_id,
                        "costType": cost_type,
                        "periodStart": period_start.isoformat(),
                        "periodEnd": period_end.isoformat(),
                        "missingDays": candidates[key]["missingDays"],
                    }
                )
            db.commit()
        return {"requeued": requeued, "candidateMonths": len(candidates)}

    def telemetry_coverage_report(
        self,
        *,
        low_coverage_threshold: float = 60.0,
        uncovered_limit: int = 25,
    ) -> dict[str, Any]:
        """Estate-wide telemetry coverage accounting for costed VMs.

        Right-sizing evidence already discloses per-VM coverage; what was
        missing is the estate answer -- how much of the fleet (and how much
        monthly spend) has no utilization evidence at all. Uncovered VMs are
        ranked by monthly cost so agent/scope rollout can be prioritized by
        the spend it would make explainable, not alphabetically.
        """
        with self.connect(read_only=True) as db:
            rows = db.execute(
                """
                WITH vms AS (
                    SELECT lower(resource_id) AS resource_id,
                           name,
                           coalesce(subscription_name, '') AS subscription_name,
                           coalesce(estimated_monthly_cost, 0) AS monthly_cost
                    FROM resources_current
                    WHERE lower(resource_type)
                        = 'microsoft.compute/virtualmachines'
                ),
                cpu AS (
                    SELECT lower(resource_id) AS resource_id,
                           source,
                           max(coverage_percent) AS coverage_percent
                    FROM telemetry_metric_summaries_current
                    WHERE lower(metric) = 'percentage cpu'
                    GROUP BY lower(resource_id), source
                )
                SELECT vms.resource_id, vms.name, vms.subscription_name,
                       vms.monthly_cost, cpu.source, cpu.coverage_percent
                FROM vms
                LEFT JOIN cpu USING (resource_id)
                """
            ).fetchall()
        vms: dict[str, dict[str, Any]] = {}
        source_counts: dict[str, dict[str, float]] = {}
        for row in rows:
            entry = vms.setdefault(
                str(row[0]),
                {
                    "resourceId": str(row[0]),
                    "name": str(row[1] or ""),
                    "subscriptionName": str(row[2] or ""),
                    "estimatedMonthlyCost": float(row[3] or 0),
                    "bestCoverage": None,
                },
            )
            if row[4] is not None:
                coverage = float(row[5] or 0)
                entry["bestCoverage"] = max(
                    entry["bestCoverage"] or 0, coverage
                )
                bucket = source_counts.setdefault(
                    str(row[4]), {"vmCount": 0, "coverageSum": 0.0}
                )
                bucket["vmCount"] += 1
                bucket["coverageSum"] += coverage
        total = len(vms)
        covered = [vm for vm in vms.values() if vm["bestCoverage"] is not None]
        uncovered = sorted(
            (vm for vm in vms.values() if vm["bestCoverage"] is None),
            key=lambda vm: -vm["estimatedMonthlyCost"],
        )
        low_coverage = [
            vm
            for vm in covered
            if (vm["bestCoverage"] or 0) < low_coverage_threshold
        ]
        total_cost = sum(vm["estimatedMonthlyCost"] for vm in vms.values())
        uncovered_cost = sum(vm["estimatedMonthlyCost"] for vm in uncovered)
        return {
            "totalVms": total,
            "coveredVms": len(covered),
            "coveredPercent": (
                round(len(covered) / total * 100, 1) if total else None
            ),
            "lowCoverageVms": len(low_coverage),
            "lowCoverageThresholdPercent": low_coverage_threshold,
            "totalMonthlyCost": round(total_cost, 2),
            "uncoveredMonthlyCost": round(uncovered_cost, 2),
            "coveredMonthlyCost": round(total_cost - uncovered_cost, 2),
            "bySource": sorted(
                (
                    {
                        "source": source,
                        "vmCount": int(bucket["vmCount"]),
                        "averageCoveragePercent": round(
                            bucket["coverageSum"] / bucket["vmCount"], 1
                        ),
                    }
                    for source, bucket in source_counts.items()
                ),
                key=lambda item: -item["vmCount"],
            ),
            "uncovered": [
                {
                    "resourceId": vm["resourceId"],
                    "name": vm["name"],
                    "subscriptionName": vm["subscriptionName"],
                    "estimatedMonthlyCost": round(
                        vm["estimatedMonthlyCost"], 2
                    ),
                }
                for vm in uncovered[:uncovered_limit]
            ],
            "uncoveredTruncated": len(uncovered) > uncovered_limit,
        }

    def slo_measurements(self, *, initial_days: int) -> dict[str, float | None]:
        """Raw values behind each SLO; None marks a probe that produced
        nothing (evaluated as ``unknown``, never as ``ok``)."""
        measurements: dict[str, float | None] = {}
        try:
            coverage = self.cost_history_coverage(initial_days=initial_days)
            measurements["cost_coverage_percent"] = coverage[
                "coveragePercent"
            ]
        except Exception:
            measurements["cost_coverage_percent"] = None
        try:
            with self.operational_connect(read_only=True) as db:
                row = db.execute(
                    """
                    WITH latest AS (
                        SELECT status,
                            row_number() OVER (
                                PARTITION BY subscription_id, cost_type
                                ORDER BY started_at DESC, run_id DESC
                            ) AS rank
                        FROM cost_history_scope_runs
                        WHERE started_at >= ? AND status <> 'running'
                    )
                    SELECT count(*) FILTER (WHERE status = 'succeeded'),
                           count(*)
                    FROM latest WHERE rank = 1
                    """,
                    [utc_now() - timedelta(days=7)],
                ).fetchone()
            succeeded, total = int(row[0] or 0), int(row[1] or 0)
            measurements["cost_scope_success_percent"] = (
                round(succeeded / total * 100, 1) if total else None
            )
        except Exception:
            measurements["cost_scope_success_percent"] = None
        try:
            stale = sum(
                1
                for item in self.source_freshness()
                if item.get("health") in ("degraded", "stale")
            )
            measurements["stale_source_count"] = float(stale)
        except Exception:
            measurements["stale_source_count"] = None
        try:
            with self.operational_connect(read_only=True) as db:
                publication = db.execute(
                    """
                    SELECT generated_at FROM analytics_publications
                    WHERE status = 'approved'
                    ORDER BY version DESC LIMIT 1
                    """
                ).fetchone()
            measurements["snapshot_age_hours"] = (
                round(
                    (utc_now() - publication[0]).total_seconds() / 3600, 1
                )
                if publication and publication[0]
                else None
            )
        except Exception:
            measurements["snapshot_age_hours"] = None
        return measurements

    def slo_report(
        self,
        *,
        initial_days: int,
        threshold_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Current SLO evaluations plus the persisted transition state."""
        from .slo import (
            DEFAULT_SLOS,
            apply_threshold_overrides,
            evaluate_slo,
        )

        definitions = apply_threshold_overrides(
            DEFAULT_SLOS, threshold_overrides
        )
        measurements = self.slo_measurements(initial_days=initial_days)
        with self.operational_connect(read_only=True) as db:
            state_rows = db.execute(
                "SELECT slo_key, state, since, last_notified FROM slo_state"
            ).fetchall()
        states = {
            str(row[0]): {
                "state": str(row[1]),
                "since": row[2].isoformat() if row[2] else None,
                "lastNotified": row[3].isoformat() if row[3] else None,
            }
            for row in state_rows
        }
        evaluations = []
        worst = "ok"
        severity = {"ok": 0, "unknown": 1, "warn": 2, "breach": 3}
        for definition in definitions:
            evaluation = evaluate_slo(
                definition, measurements.get(definition.key)
            )
            evaluation["tracked"] = states.get(definition.key)
            evaluations.append(evaluation)
            if severity[evaluation["state"]] > severity[worst]:
                worst = evaluation["state"]
        return {
            "generatedAt": utc_now().isoformat(),
            "worstState": worst,
            "objectives": evaluations,
        }

    def cost_reconciliation(
        self, _operational_db: Any | None = None
    ) -> dict[str, Any]:
        """Return one comparable coverage contract for every Azure cost dataset."""
        integration = self.integration(_operational_db=_operational_db)
        configured = [
            {
                "id": item["subscriptionId"].lower(),
                "name": item.get("label") or item["subscriptionId"],
            }
            for item in integration.get("subscriptions", [])
            if item.get("subscriptionId")
        ]
        sources = [
            "ActualCost",
            "AmortizedCost",
            "DailyActualCost",
            "DailyAmortizedCost",
            "CommitmentCoverage",
        ]
        with self.connect(read_only=True) as db:
            state_rows = db.execute(
                """
                WITH ranked AS (
                    SELECT *,
                        row_number() OVER (
                            PARTITION BY source, scope_id
                            ORDER BY observed_at DESC, snapshot_id DESC
                        ) AS rank
                    FROM source_sync_state
                    WHERE source IN (
                        'ActualCost', 'AmortizedCost',
                        'DailyActualCost', 'DailyAmortizedCost',
                        'CommitmentCoverage', 'FocusCost'
                    )
                )
                SELECT source, scope_id, snapshot_id, observed_at, row_count
                FROM ranked WHERE rank = 1
                """
            ).fetchall()
            current_rows = db.execute(
                """
                SELECT cost_type, subscription_id, count(*), sum(amount),
                       count(DISTINCT NULLIF(currency, '')),
                       any_value(NULLIF(currency, '')),
                       min(period_start), max(period_end)
                FROM costs_current
                GROUP BY cost_type, subscription_id
                """
            ).fetchall()
            daily_rows = db.execute(
                """
                SELECT 'Daily' || cost_type, subscription_id, count(*),
                       sum(amount), count(DISTINCT NULLIF(currency, '')),
                       any_value(NULLIF(currency, '')),
                       min(usage_date), max(usage_date)
                FROM daily_cost_history
                GROUP BY cost_type, subscription_id
                """
            ).fetchall()
            commitment_rows = db.execute(
                """
                SELECT 'CommitmentCoverage', subscription_id, count(*),
                       sum(amount), count(DISTINCT NULLIF(currency, '')),
                       any_value(NULLIF(currency, '')),
                       min(period_start), max(period_end)
                FROM commitment_costs_current
                GROUP BY subscription_id
                """
            ).fetchall()
            focus_rows = db.execute(
                """
                WITH current AS (
                    SELECT *, row_number() OVER (
                        PARTITION BY subscription_id, period_start, period_end
                        ORDER BY imported_at DESC
                    ) AS rank
                    FROM focus_export_manifests
                    WHERE status = 'imported'
                )
                SELECT 'FocusCost', subscription_id, sum(row_count),
                       sum(effective_cost),
                       count(DISTINCT NULLIF(currency, '')),
                       any_value(NULLIF(currency, '')),
                       min(period_start), max(period_end)
                FROM current
                WHERE rank = 1
                GROUP BY subscription_id
                """
            ).fetchall()
        has_focus_data = bool(focus_rows) or any(
            str(row[0]) == "FocusCost" for row in state_rows
        )
        if self.focus_cost_enabled and (
            self.focus_cost_required or has_focus_data
        ):
            sources.append("FocusCost")
        with self._optional_operational_connect(_operational_db) as db:
            attempt_rows = db.execute(
                """
                WITH current_attempts AS (
                    SELECT source, scope_id, started_at, completed_at, status,
                           retained_last_good, message, status_code,
                           retry_after_seconds, next_retry_at,
                        row_number() OVER (
                            PARTITION BY source, scope_id
                            ORDER BY started_at DESC, sync_id DESC
                        ) AS rank
                    FROM sync_source_runs
                    WHERE source IN (
                        'ActualCost', 'AmortizedCost', 'CommitmentCoverage'
                    )
                ),
                daily_attempts AS (
                    SELECT
                        'Daily' || cost_type AS source,
                        subscription_id AS scope_id,
                        started_at,
                        completed_at,
                        status,
                        retained_last_good,
                        message,
                        status_code,
                        CAST(NULL AS DOUBLE) AS retry_after_seconds,
                        CAST(NULL AS TIMESTAMPTZ) AS next_retry_at,
                        row_number() OVER (
                            PARTITION BY subscription_id, cost_type
                            ORDER BY started_at DESC, run_id DESC
                        ) AS rank
                    FROM cost_history_scope_runs
                )
                SELECT source, scope_id, started_at, completed_at, status,
                       retained_last_good, message, status_code,
                       retry_after_seconds, next_retry_at
                FROM current_attempts WHERE rank = 1
                UNION ALL
                SELECT source, scope_id, started_at, completed_at, status,
                       retained_last_good, message, status_code,
                       retry_after_seconds, next_retry_at
                FROM daily_attempts WHERE rank = 1
                """
            ).fetchall()

        state = {
            (str(row[0]), str(row[1]).lower()): {
                "snapshotId": row[2],
                "lastSuccessfulAt": row[3],
                "successfulRowCount": int(row[4] or 0),
            }
            for row in state_rows
        }
        attempts = {
            (str(row[0]), str(row[1]).lower()): {
                "lastAttemptAt": row[2],
                "lastCompletedAt": row[3],
                "status": row[4],
                "retainedLastGood": bool(row[5]),
                "message": row[6] or "",
                "statusCode": row[7],
                "retryAfterSeconds": row[8],
                "nextRetryAt": row[9],
            }
            for row in attempt_rows
        }
        aggregates = {
            (str(row[0]), str(row[1]).lower()): {
                "rowCount": int(row[2] or 0),
                "amount": round(float(row[3] or 0), 2),
                "currency": (
                    "Mixed" if int(row[4] or 0) > 1 else (row[5] or "")
                ),
                "periodStart": row[6],
                "periodEnd": row[7],
            }
            for row in [*current_rows, *daily_rows, *commitment_rows, *focus_rows]
        }
        month_start = utc_now().date().replace(day=1)
        labels = {
            "ActualCost": "Current actual cost",
            "AmortizedCost": "Current amortized cost",
            "DailyActualCost": "Daily actual history",
            "DailyAmortizedCost": "Daily amortized history",
            "CommitmentCoverage": "Commitment coverage",
            "FocusCost": "FOCUS cost and usage",
        }
        grains = {
            "ActualCost": "Month-to-date resource snapshot",
            "AmortizedCost": "Month-to-date resource snapshot",
            "DailyActualCost": "Daily resource and service history",
            "DailyAmortizedCost": "Daily resource and service history",
            "CommitmentCoverage": "Month-to-date meter and pricing-model summary",
            "FocusCost": "Charge-level FOCUS v1.0 cost and usage ledger",
        }
        datasets: list[dict[str, Any]] = []
        for source in sources:
            scopes: list[dict[str, Any]] = []
            for subscription in configured:
                key = (source, subscription["id"])
                successful = state.get(key)
                attempt = attempts.get(key, {})
                if (
                    successful
                    and source in {"DailyActualCost", "DailyAmortizedCost"}
                    and str(successful["snapshotId"]).startswith("focus-")
                ):
                    attempt = {
                        "status": "succeeded",
                        "retainedLastGood": False,
                        "message": "Governed by the latest FOCUS export.",
                    }
                aggregate = aggregates.get(key, {})
                period_end = aggregate.get("periodEnd")
                # "Current" honors the 24-48h billing latency across the
                # month boundary: strict calendar-month membership made
                # every scope read 0/N current on the 1st-2nd (data through
                # Jul 31 is ON the finalized horizon on Aug 2, not stale)
                # until the new month's first collection landed.
                current_threshold = min(
                    month_start,
                    utc_now().date() - timedelta(days=2),
                )
                current_period = bool(
                    successful
                    and (
                        (period_end and period_end >= current_threshold)
                        or (
                            not period_end
                            and successful["lastSuccessfulAt"].date()
                            >= current_threshold
                        )
                    )
                )
                status = attempt.get("status") or (
                    "succeeded" if successful else "not_attempted"
                )
                scopes.append(
                    {
                        "subscriptionId": subscription["id"],
                        "subscriptionName": subscription["name"],
                        "status": status,
                        "available": successful is not None,
                        "currentPeriod": current_period,
                        "retainedLastGood": bool(
                            attempt.get("retainedLastGood", False)
                        ),
                        "rowCount": aggregate.get(
                            "rowCount",
                            successful["successfulRowCount"] if successful else 0,
                        ),
                        "amount": aggregate.get("amount"),
                        "currency": aggregate.get("currency", ""),
                        "periodStart": (
                            aggregate["periodStart"].isoformat()
                            if aggregate.get("periodStart")
                            else None
                        ),
                        "periodEnd": (
                            period_end.isoformat() if period_end else None
                        ),
                        "lastSuccessfulAt": (
                            successful["lastSuccessfulAt"].isoformat()
                            if successful
                            else None
                        ),
                        "lastAttemptAt": (
                            attempt["lastAttemptAt"].isoformat()
                            if attempt.get("lastAttemptAt")
                            else None
                        ),
                        "message": attempt.get("message", ""),
                        "statusCode": attempt.get("statusCode"),
                        "retryAfterSeconds": attempt.get(
                            "retryAfterSeconds"
                        ),
                        "nextRetryAt": (
                            attempt["nextRetryAt"].isoformat()
                            if attempt.get("nextRetryAt")
                            else None
                        ),
                    }
                )
            currencies = {
                item["currency"] for item in scopes if item["currency"]
            }
            available_scopes = sum(1 for item in scopes if item["available"])
            current_scopes = sum(1 for item in scopes if item["currentPeriod"])
            failed_scopes = sum(
                1 for item in scopes if item["status"] == "failed"
            )
            retained_scopes = sum(
                1 for item in scopes if item["retainedLastGood"]
            )
            successful_times = [
                item["lastSuccessfulAt"]
                for item in scopes
                if item["lastSuccessfulAt"]
            ]
            periods_start = [
                item["periodStart"] for item in scopes if item["periodStart"]
            ]
            periods_end = [
                item["periodEnd"] for item in scopes if item["periodEnd"]
            ]
            datasets.append(
                {
                    "source": source,
                    "label": labels[source],
                    "grain": grains[source],
                    "configuredScopes": len(scopes),
                    "availableScopes": available_scopes,
                    "currentPeriodScopes": current_scopes,
                    "failedScopes": failed_scopes,
                    "retainedScopes": retained_scopes,
                    "complete": bool(scopes)
                    and available_scopes == len(scopes),
                    "currentPeriodComplete": bool(scopes)
                    and current_scopes == len(scopes),
                    "rowCount": sum(item["rowCount"] for item in scopes),
                    "amount": round(
                        sum(
                            float(item["amount"] or 0)
                            for item in scopes
                            if item["amount"] is not None
                        ),
                        2,
                    ),
                    "currency": (
                        "Mixed"
                        if len(currencies) > 1 or "Mixed" in currencies
                        else next(iter(currencies), "")
                    ),
                    "periodStart": min(periods_start) if periods_start else None,
                    "periodEnd": max(periods_end) if periods_end else None,
                    "lastSuccessfulAt": (
                        max(successful_times) if successful_times else None
                    ),
                    "scopes": scopes,
                }
            )
        return {
            "asOf": utc_now().isoformat(),
            "configuredSubscriptions": len(configured),
            "datasets": datasets,
            "source": "azure_cost_management_query",
            "warning": (
                "Current snapshots, daily history, and commitment evidence are "
                "independent datasets. Totals are comparable only when their "
                "period, currency, and subscription coverage match."
            ),
        }

    def store_daily_cost_scope(
        self,
        snapshot_id: str,
        subscription_id: str,
        cost_type: str,
        records: list[dict[str, Any]],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> int:
        observed_at = utc_now()
        incoming_focus = any(
            item.get("source") == "azure_focus_export" for item in records
        )
        if records and not incoming_focus:
            with self.connect(read_only=True) as db:
                focus_periods = db.execute(
                    """
                    SELECT period_start, period_end
                    FROM focus_export_manifests
                    WHERE subscription_id = ? AND status = 'imported'
                    """,
                    [subscription_id.lower()],
                ).fetchall()

            # usageDate arrives as an ISO string from the Query API path while
            # DuckDB returns DATE columns as datetime.date; comparing them
            # raises TypeError. This only fires for subscriptions that HAVE
            # imported FOCUS manifests, which made every Query-API cost sync
            # fail for exactly those subscriptions (all 4 original FOCUS subs,
            # both cost types, every run) while the rest of the estate
            # succeeded -- and would have spread to each newly provisioned
            # FOCUS subscription as its first manifest imported.
            def _usage_date(value: Any) -> date:
                return (
                    value
                    if isinstance(value, date)
                    else date.fromisoformat(str(value)[:10])
                )

            records = [
                item
                for item in records
                if not any(
                    period_start <= _usage_date(item["usageDate"]) <= period_end
                    for period_start, period_end in focus_periods
                )
            ]
        source = f"Daily{cost_type}"
        with self.connect() as db:
            db.execute("BEGIN TRANSACTION")
            try:
                if start_date and end_date:
                    # Azure can correct or remove billed rows inside the
                    # rolling window. Replace the complete successful scope so
                    # an omitted row cannot survive as stale cost.
                    db.execute(
                        """
                        DELETE FROM daily_cost_history
                        WHERE subscription_id = ? AND cost_type = ?
                          AND usage_date BETWEEN ? AND ?
                          AND (? OR source <> 'azure_focus_export')
                        """,
                        [
                            subscription_id.lower(),
                            cost_type,
                            start_date,
                            end_date,
                            incoming_focus,
                        ],
                    )
                inserted_rows = 0
                if records:
                    for offset in range(0, len(records), _DUCKDB_INSERT_BATCH_SIZE):
                        batch = [
                            [
                                snapshot_id,
                                observed_at,
                                item["usageDate"],
                                cost_type,
                                subscription_id.lower(),
                                str(item.get("resourceId", "")).lower(),
                                canonical_service_name(
                                    item.get("serviceName", "")
                                ),
                                item.get("amount", 0),
                                item.get("currency", ""),
                                item.get("source", "azure_cost_management_query"),
                            ]
                            for item in records[
                                offset : offset + _DUCKDB_INSERT_BATCH_SIZE
                            ]
                        ]
                        db.executemany(
                            """
                            INSERT OR REPLACE INTO daily_cost_history VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                            )
                            """,
                            batch,
                        )
                        inserted_rows += len(batch)
                db.execute(
                    "INSERT INTO source_sync_state VALUES (?, ?, ?, ?, ?)",
                    [
                        snapshot_id,
                        observed_at,
                        source,
                        subscription_id.lower(),
                        inserted_rows,
                    ],
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return inserted_rows

    def store_monthly_cost_scope(
        self,
        snapshot_id: str,
        subscription_id: str,
        cost_type: str,
        records: list[dict[str, Any]],
        *,
        start_month: date,
        end_month: date,
    ) -> int:
        """Replace one scope's monthly totals inside the fetched window.

        Months outside the window are left alone, so the table accumulates
        history beyond Cost Management's thirteen-month lookback as time
        passes instead of being trimmed to it.
        """
        observed_at = utc_now()
        with self.connect() as db:
            db.execute("BEGIN TRANSACTION")
            try:
                db.execute(
                    """
                    DELETE FROM monthly_cost_history
                    WHERE subscription_id = ? AND cost_type = ?
                      AND month BETWEEN ? AND ?
                    """,
                    [
                        subscription_id.lower(),
                        cost_type,
                        start_month,
                        end_month,
                    ],
                )
                for item in records:
                    db.execute(
                        """
                        INSERT OR REPLACE INTO monthly_cost_history VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        [
                            snapshot_id,
                            observed_at,
                            item["month"],
                            cost_type,
                            subscription_id.lower(),
                            float(item.get("amount") or 0),
                            str(item.get("currency") or ""),
                            str(
                                item.get("source")
                                or "azure_cost_management_query"
                            ),
                        ],
                    )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return len(records)

    def monthly_cost_scope_fresh(
        self,
        subscription_id: str,
        cost_type: str,
        current_month: date,
        max_age_hours: float = 20,
    ) -> bool:
        """Whether this scope's current-month total was stored recently.

        The collector dies mid-run often enough that the monthly phase must
        make new progress every attempt: fresh scopes are skipped so a rerun
        spends its requests on the scopes a previous death never reached.
        """
        with self.connect(read_only=True) as db:
            row = db.execute(
                """
                SELECT max(observed_at) FROM monthly_cost_history
                WHERE subscription_id = ? AND cost_type = ? AND month = ?
                """,
                [subscription_id.lower(), cost_type, current_month],
            ).fetchone()
        if not row or row[0] is None:
            return False
        observed = row[0]
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return (utc_now() - observed).total_seconds() < max_age_hours * 3600

    def monthly_cost_scope_bounds(
        self, subscription_id: str, cost_type: str
    ) -> tuple[date, date] | None:
        with self.connect(read_only=True) as db:
            row = db.execute(
                """
                SELECT min(month), max(month) FROM monthly_cost_history
                WHERE subscription_id = ? AND cost_type = ?
                """,
                [subscription_id.lower(), cost_type],
            ).fetchone()
        if not row or row[0] is None:
            return None
        return row[0], row[1]

    def monthly_cost_totals(self, cost_type: str) -> dict[str, Any]:
        """Estate month totals in the dominant currency, for the FY outlook."""
        with self.connect(read_only=True) as db:
            currency_rows = db.execute(
                """
                SELECT currency, sum(abs(amount)) AS weight
                FROM monthly_cost_history WHERE cost_type = ?
                GROUP BY currency ORDER BY weight DESC
                """,
                [cost_type],
            ).fetchall()
            if not currency_rows:
                return {"currency": "", "months": {}, "otherCurrencies": []}
            currency = str(currency_rows[0][0] or "")
            rows = db.execute(
                """
                SELECT month, sum(amount) FROM monthly_cost_history
                WHERE cost_type = ? AND currency = ?
                GROUP BY month ORDER BY month
                """,
                [cost_type, currency],
            ).fetchall()
        return {
            "currency": currency,
            "months": {row[0]: float(row[1] or 0) for row in rows},
            "otherCurrencies": [
                str(row[0] or "") for row in currency_rows[1:] if row[0]
            ],
        }

    def focus_manifest_imported(self, manifest_path: str) -> bool:
        with self.connect(read_only=True) as db:
            row = db.execute(
                """
                SELECT count(*)
                FROM focus_export_manifests
                WHERE manifest_path = ? AND status = 'imported'
                """,
                [manifest_path],
            ).fetchone()
        return bool(row and row[0])

    def start_focus_import(self, run_id: str) -> None:
        with self.operational_connect() as db:
            db.execute(
                """
                INSERT INTO focus_import_runs (
                    run_id, started_at, completed_at, status,
                    manifest_count, charge_count, message
                ) VALUES (?, ?, NULL, 'running', 0, 0, '')
                ON CONFLICT (run_id) DO UPDATE SET
                    started_at = excluded.started_at,
                    completed_at = NULL,
                    status = 'running',
                    manifest_count = 0,
                    charge_count = 0,
                    message = ''
                """,
                [run_id, utc_now()],
            )

    def finish_focus_import(
        self,
        run_id: str,
        *,
        status: str,
        manifest_count: int,
        charge_count: int,
        message: str = "",
    ) -> None:
        with self.operational_connect() as db:
            db.execute(
                """
                UPDATE focus_import_runs
                SET completed_at = ?, status = ?, manifest_count = ?,
                    charge_count = ?, message = ?
                WHERE run_id = ?
                """,
                [
                    utc_now(),
                    status,
                    manifest_count,
                    charge_count,
                    message,
                    run_id,
                ],
            )

    def store_focus_manifest(
        self,
        import_run_id: str,
        manifest_path: str,
        manifest: dict[str, Any],
        csv_files: list[tuple[str, Path]],
    ) -> int:
        """Atomically retain one FOCUS export and promote governed daily totals."""
        export = manifest.get("exportConfig") or {}
        run = manifest.get("runInfo") or {}
        resource_id = str(export.get("resourceId") or "")
        parts = [part for part in resource_id.split("/") if part]
        subscription_id = (
            parts[parts.index("subscriptions") + 1].lower()
            if "subscriptions" in parts
            else ""
        )
        export_name = str(export.get("exportName") or "")
        export_run_id = str(run.get("runId") or "")
        manifest_id = f"{subscription_id}:{export_name}:{export_run_id}"
        period_start = date.fromisoformat(str(run["startDate"])[:10])
        period_end = date.fromisoformat(str(run["endDate"])[:10])
        submitted_at = parse_iso_timestamp(run.get("submittedTime"))
        imported_at = utc_now()
        with self.connect() as db:
            db.execute("BEGIN TRANSACTION")
            try:
                prior = db.execute(
                    """
                    SELECT manifest_id
                    FROM focus_export_manifests
                    WHERE subscription_id = ?
                      AND period_start = ? AND period_end = ?
                      AND status = 'imported' AND manifest_id <> ?
                    """,
                    [subscription_id, period_start, period_end, manifest_id],
                ).fetchall()
                for (prior_id,) in prior:
                    db.execute(
                        "DELETE FROM focus_cost_charges WHERE manifest_id = ?",
                        [prior_id],
                    )
                db.execute(
                    """
                    UPDATE focus_export_manifests SET status = 'superseded'
                    WHERE subscription_id = ?
                      AND period_start = ? AND period_end = ?
                      AND manifest_id <> ? AND status = 'imported'
                    """,
                    [subscription_id, period_start, period_end, manifest_id],
                )
                db.execute(
                    "DELETE FROM focus_cost_charges WHERE manifest_id = ?",
                    [manifest_id],
                )
                for blob_name, csv_path in csv_files:
                    db.execute(
                        """
                        CREATE OR REPLACE TEMP TABLE focus_stage AS
                        SELECT *, row_number() OVER () AS flux_row_number
                        FROM read_csv(
                            ?, header = true, all_varchar = true,
                            auto_detect = true, ignore_errors = false
                        )
                        """,
                        [str(csv_path)],
                    )
                    db.execute(
                        """
                        INSERT INTO focus_cost_charges
                        SELECT
                            sha256(? || ':' || ? || ':' ||
                                   CAST(flux_row_number AS VARCHAR)),
                            ?,
                            try_cast(replace(replace(
                                ChargePeriodStart, 'T', ' '
                            ), 'Z', '') AS TIMESTAMPTZ),
                            try_cast(replace(replace(
                                ChargePeriodEnd, 'T', ' '
                            ), 'Z', '') AS TIMESTAMPTZ),
                            try_cast(replace(replace(
                                BillingPeriodStart, 'T', ' '
                            ), 'Z', '') AS TIMESTAMPTZ),
                            try_cast(replace(replace(
                                BillingPeriodEnd, 'T', ' '
                            ), 'Z', '') AS TIMESTAMPTZ),
                            coalesce(try_cast(BilledCost AS DOUBLE), 0),
                            coalesce(try_cast(EffectiveCost AS DOUBLE), 0),
                            try_cast(ContractedCost AS DOUBLE),
                            try_cast(ListCost AS DOUBLE),
                            coalesce(BillingCurrency, ''),
                            coalesce(ChargeCategory, ''),
                            coalesce(ChargeClass, ''),
                            coalesce(ChargeFrequency, ''),
                            coalesce(ChargeDescription, ''),
                            coalesce(PricingCategory, ''),
                            try_cast(ConsumedQuantity AS DOUBLE),
                            coalesce(ConsumedUnit, ''),
                            try_cast(PricingQuantity AS DOUBLE),
                            coalesce(PricingUnit, ''),
                            try_cast(ContractedUnitPrice AS DOUBLE),
                            try_cast(ListUnitPrice AS DOUBLE),
                            coalesce(CommitmentDiscountId, ''),
                            coalesce(CommitmentDiscountName, ''),
                            coalesce(CommitmentDiscountCategory, ''),
                            coalesce(CommitmentDiscountType, ''),
                            coalesce(ServiceCategory, ''),
                            coalesce(ServiceName, ''),
                            lower(coalesce(ResourceId, '')),
                            coalesce(ResourceName, ''),
                            coalesce(ResourceType, ''),
                            coalesce(x_ResourceGroupName, ''),
                            -- Azure FOCUS exports carry SubAccountId as the
                            -- full ARM path (/subscriptions/<guid>), while
                            -- every other Flux path keys on the bare GUID.
                            -- Normalize here so cost, inventory, budgets,
                            -- and anomaly baselines share one key space.
                            regexp_replace(
                                lower(coalesce(SubAccountId, ?)),
                                '^/subscriptions/', ''
                            ),
                            coalesce(SubAccountName, ''),
                            coalesce(ProviderName, ''),
                            coalesce(PublisherName, ''),
                            coalesce(RegionName, ''),
                            coalesce(SkuId, ''),
                            coalesce(SkuPriceId, ''),
                            coalesce(x_SkuMeterId, ''),
                            coalesce(x_SkuMeterName, ''),
                            coalesce(x_SkuMeterCategory, ''),
                            coalesce(x_SkuMeterSubcategory, ''),
                            coalesce(try_cast(Tags AS JSON), '{}'::JSON),
                            to_json(focus_stage)
                        FROM focus_stage
                        WHERE try_cast(replace(replace(
                                  ChargePeriodStart, 'T', ' '
                              ), 'Z', '') AS TIMESTAMPTZ)
                              IS NOT NULL
                        """,
                        [
                            manifest_id,
                            blob_name,
                            manifest_id,
                            f"/subscriptions/{subscription_id}",
                        ],
                    )
                aggregate = db.execute(
                    """
                    SELECT count(*), coalesce(sum(billed_cost), 0),
                           coalesce(sum(effective_cost), 0),
                           any_value(billing_currency),
                           any_value(subscription_name)
                    FROM focus_cost_charges
                    WHERE manifest_id = ?
                    """,
                    [manifest_id],
                ).fetchone()
                row_count = int(aggregate[0] or 0)
                expected_rows = int(manifest.get("dataRowCount") or 0)
                if row_count != expected_rows:
                    raise ValueError(
                        f"FOCUS row reconciliation failed for {manifest_path}: "
                        f"expected {expected_rows:,}, parsed {row_count:,}."
                    )
                currency = aggregate[3] or ""
                subscription_name = aggregate[4] or subscription_id
                db.execute(
                    "DELETE FROM focus_export_manifests WHERE manifest_id = ?",
                    [manifest_id],
                )
                db.execute(
                    """
                    INSERT INTO focus_export_manifests VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'imported', ?,
                        ?, ?, ?, ?, ?, ''
                    )
                    """,
                    [
                        manifest_id,
                        import_run_id,
                        manifest_path,
                        export_name,
                        export_run_id,
                        subscription_id,
                        subscription_name,
                        period_start,
                        period_end,
                        submitted_at,
                        imported_at,
                        str(export.get("dataVersion") or ""),
                        row_count,
                        int(manifest.get("byteCount") or 0),
                        currency,
                        float(aggregate[1] or 0),
                        float(aggregate[2] or 0),
                    ],
                )
                for cost_type, amount_column in (
                    ("ActualCost", "billed_cost"),
                    ("AmortizedCost", "effective_cost"),
                ):
                    db.execute(
                        """
                        DELETE FROM daily_cost_history
                        WHERE subscription_id = ? AND cost_type = ?
                          AND usage_date BETWEEN ? AND ?
                        """,
                        [subscription_id, cost_type, period_start, period_end],
                    )
                    canonical_service = _canonical_service_name_sql(
                        "service_name"
                    )
                    db.execute(
                        f"""
                        INSERT INTO daily_cost_history
                        SELECT ?, ?, CAST(charge_period_start AS DATE), ?,
                               subscription_id,
                               CASE
                                 WHEN resource_id <> '' THEN resource_id
                                 ELSE '/subscriptions/' || subscription_id
                               END,
                               {canonical_service}, sum({amount_column}),
                               billing_currency, 'azure_focus_export'
                        FROM focus_cost_charges
                        WHERE manifest_id = ?
                        GROUP BY CAST(charge_period_start AS DATE),
                                 subscription_id,
                                 CASE
                                   WHEN resource_id <> '' THEN resource_id
                                   ELSE '/subscriptions/' || subscription_id
                                 END,
                                 {canonical_service}, billing_currency
                        """,
                        [
                            import_run_id,
                            imported_at,
                            cost_type,
                            manifest_id,
                        ],
                    )
                    source = f"Daily{cost_type}"
                    promoted = db.execute(
                        """
                        SELECT count(*) FROM daily_cost_history
                        WHERE snapshot_id = ? AND subscription_id = ?
                          AND cost_type = ?
                        """,
                        [import_run_id, subscription_id, cost_type],
                    ).fetchone()[0]
                    db.execute(
                        "INSERT INTO source_sync_state VALUES (?, ?, ?, ?, ?)",
                        [
                            import_run_id,
                            imported_at,
                            source,
                            subscription_id,
                            int(promoted or 0),
                        ],
                    )
                db.execute(
                    "INSERT INTO source_sync_state VALUES (?, ?, ?, ?, ?)",
                    [
                        import_run_id,
                        imported_at,
                        "FocusCost",
                        subscription_id,
                        row_count,
                    ],
                )
                db.execute("COMMIT")
                return row_count
            except Exception:
                db.execute("ROLLBACK")
                raise

    def compute_cost_anomalies(
        self,
        run_id: str,
        *,
        latency_days: int,
        minimum_history_days: int,
        minimum_baseline_points: int,
        baseline_weeks: int,
        threshold_k: float,
        minimum_increase: float,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        evaluated_at = utc_now()
        cutoff = (as_of or evaluated_at.date()) - timedelta(
            days=max(latency_days, 0)
        )
        history_start = cutoff - timedelta(
            days=max(
                baseline_weeks * 7 + 7,
                minimum_history_days + 7,
            )
        )
        with self.connect(read_only=True) as db:
            evaluation_rows = db.execute(
                """
                SELECT cost_type, subscription_id, max(usage_date)
                FROM daily_cost_history
                WHERE usage_date <= ?
                GROUP BY cost_type, subscription_id
                """,
                [cutoff],
            ).fetchall()
        evaluation_dates = {
            (row[0], row[1]): row[2] for row in evaluation_rows if row[2]
        }

        stored_rows: list[list[Any]] = []
        evaluated_count = 0
        anomaly_count = 0
        warming_count = 0

        queries = {
            "subscription": """
                WITH names AS (
                    SELECT subscription_id,
                           any_value(NULLIF(subscription_name, '')) AS name
                    FROM resources_current GROUP BY subscription_id
                )
                SELECT cost.cost_type, cost.subscription_id,
                       cost.subscription_id AS scope_id,
                       '' AS resource_id,
                       COALESCE(names.name, cost.subscription_id) AS resource_name,
                       '' AS resource_type, '' AS resource_group,
                       '' AS service_name, cost.currency, cost.usage_date,
                       sum(cost.amount) AS amount
                FROM daily_cost_history AS cost
                LEFT JOIN names USING (subscription_id)
                WHERE cost.usage_date BETWEEN ? AND ?
                GROUP BY cost.cost_type, cost.subscription_id, names.name,
                         cost.currency, cost.usage_date
                ORDER BY cost.cost_type, cost.subscription_id, scope_id,
                         cost.currency, cost.usage_date
            """,
            "service": """
                SELECT cost_type, subscription_id,
                       subscription_id || '|' ||
                           COALESCE(NULLIF(service_name, ''), 'Unallocated')
                           AS scope_id,
                       '' AS resource_id,
                       COALESCE(NULLIF(service_name, ''), 'Unallocated')
                           AS resource_name,
                       '' AS resource_type, '' AS resource_group,
                       COALESCE(NULLIF(service_name, ''), 'Unallocated')
                           AS service_name,
                       currency, usage_date, sum(amount) AS amount
                FROM daily_cost_history
                WHERE usage_date BETWEEN ? AND ?
                GROUP BY cost_type, subscription_id, service_name,
                         currency, usage_date
                ORDER BY cost_type, subscription_id, scope_id,
                         currency, usage_date
            """,
            "resource": """
                SELECT cost.cost_type, cost.subscription_id,
                       cost.resource_id AS scope_id, cost.resource_id,
                       COALESCE(NULLIF(resource.name, ''), cost.resource_id)
                           AS resource_name,
                       COALESCE(resource.resource_type, '') AS resource_type,
                       COALESCE(resource.resource_group, '') AS resource_group,
                       '' AS service_name,
                       cost.currency, cost.usage_date, sum(cost.amount) AS amount
                FROM daily_cost_history AS cost
                LEFT JOIN resources_current AS resource
                  ON lower(resource.resource_id) = cost.resource_id
                WHERE cost.usage_date BETWEEN ? AND ?
                  AND cost.resource_id <> ''
                GROUP BY cost.cost_type, cost.subscription_id, cost.resource_id,
                         resource.name, resource.resource_type,
                         resource.resource_group, cost.currency,
                         cost.usage_date
                ORDER BY cost.cost_type, cost.subscription_id, scope_id,
                         cost.currency, cost.usage_date
            """,
        }

        def append_result(
            scope_type: str,
            identity: tuple[Any, ...],
            amounts: dict[date, float],
        ) -> None:
            nonlocal evaluated_count, anomaly_count, warming_count
            (
                cost_type,
                subscription_id,
                scope_id,
                resource_id,
                resource_name,
                resource_type,
                resource_group,
                service_name,
                currency,
            ) = identity
            evaluation_date = evaluation_dates.get(
                (cost_type, subscription_id)
            )
            if not evaluation_date:
                return
            result = evaluate_cost_series(
                amounts,
                evaluation_date,
                minimum_history_days=minimum_history_days,
                minimum_baseline_points=minimum_baseline_points,
                baseline_weeks=baseline_weeks,
                threshold_k=threshold_k,
                minimum_increase=minimum_increase,
            )
            evaluated_count += 1
            if result["status"] == "anomalous":
                anomaly_count += 1
            elif result["status"] == "warming_up":
                warming_count += 1
            if result["status"] == "normal" or (
                result["status"] == "warming_up" and scope_type == "resource"
            ):
                return
            stored_rows.append(
                [
                    run_id,
                    evaluated_at,
                    evaluation_date,
                    cost_type,
                    scope_type,
                    scope_id,
                    subscription_id,
                    resource_id,
                    resource_name,
                    resource_type,
                    resource_group,
                    service_name,
                    result["currentAmount"],
                    result["baselinePoints"],
                    result["baselineMedian"],
                    result["mad"],
                    result["kScore"],
                    result["previousWeekAmount"],
                    result["absoluteChange"],
                    result["percentChange"],
                    result["status"],
                    result["severity"],
                    currency,
                    result["reason"],
                    COST_ANOMALY_METHOD_VERSION,
                ]
            )

        for scope_type, query in queries.items():
            current_identity: tuple[Any, ...] | None = None
            amounts: dict[date, float] = {}
            with self.connect(read_only=True) as db:
                cursor = db.execute(query, [history_start, cutoff])
                while True:
                    batch = cursor.fetchmany(10_000)
                    if not batch:
                        break
                    for row in batch:
                        identity = tuple(row[:9])
                        if (
                            current_identity is not None
                            and identity != current_identity
                        ):
                            append_result(
                                scope_type, current_identity, amounts
                            )
                            amounts = {}
                        current_identity = identity
                        amounts[row[9]] = float(row[10] or 0)
            if current_identity is not None:
                append_result(scope_type, current_identity, amounts)

        maximum_evaluation_date = (
            max(evaluation_dates.values()) if evaluation_dates else None
        )
        message = (
            f"Evaluated {evaluated_count:,} daily cost scopes: "
            f"{anomaly_count:,} anomalous and {warming_count:,} warming up."
        )
        with self.connect() as db:
            db.execute("BEGIN TRANSACTION")
            try:
                db.execute(
                    """
                    INSERT INTO cost_anomaly_runs VALUES (
                        ?, ?, 'succeeded', ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        run_id,
                        evaluated_at,
                        maximum_evaluation_date,
                        evaluated_count,
                        anomaly_count,
                        warming_count,
                        message,
                        COST_ANOMALY_METHOD_VERSION,
                    ],
                )
                if stored_rows:
                    db.executemany(
                        """
                        INSERT INTO cost_anomaly_snapshots VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        stored_rows,
                    )
                db.execute(
                    "INSERT INTO source_sync_state VALUES (?, ?, ?, ?, ?)",
                    [
                        run_id,
                        evaluated_at,
                        "CostAnomalies",
                        "configured-subscriptions",
                        anomaly_count,
                    ],
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return {
            "runId": run_id,
            "evaluationDate": maximum_evaluation_date,
            "evaluatedCount": evaluated_count,
            "anomalyCount": anomaly_count,
            "warmingCount": warming_count,
            "message": message,
        }

    def compute_opportunity_confidence(self, snapshot_id: str) -> int:
        computed_at = utc_now()
        with self.connect(read_only=True) as db:
            valid_snapshots = db.execute(
                """
                SELECT source, snapshot_id, max(observed_at) AS observed_at
                FROM source_sync_state
                WHERE source IN ('AzureAdvisor', 'FluxIntelligence')
                GROUP BY source, snapshot_id
                ORDER BY source, observed_at, snapshot_id
                """
            ).fetchall()
            occurrences = db.execute(
                """
                WITH valid AS (
                    SELECT DISTINCT source, snapshot_id
                    FROM source_sync_state
                    WHERE source IN ('AzureAdvisor', 'FluxIntelligence')
                ),
                occurrence AS (
                    SELECT
                        'AzureAdvisor' AS source,
                        advisor.snapshot_id,
                        advisor.observed_at,
                        advisor.resource_id,
                        CASE
                            WHEN lower(advisor.problem || ' ' || advisor.solution)
                                LIKE '%unattached%disk%' THEN 'unattached_disk'
                            WHEN lower(advisor.problem || ' ' || advisor.solution)
                                LIKE '%underutilized%virtual machine%'
                                THEN 'compute_shutdown'
                            WHEN lower(advisor.problem || ' ' || advisor.solution)
                                LIKE '%app service plan%'
                                THEN 'empty_app_service_plan'
                            ELSE 'advisor_' ||
                                lower(replace(advisor.category, ' ', '_'))
                        END AS family
                    FROM advisor_recommendation_snapshots AS advisor
                    JOIN valid
                      ON valid.snapshot_id = advisor.snapshot_id
                     AND valid.source = 'AzureAdvisor'

                    UNION ALL

                    SELECT
                        'FluxIntelligence' AS source,
                        finding.snapshot_id,
                        finding.observed_at,
                        finding.resource_id,
                        CASE finding.rule_id
                            WHEN 'stopped_allocated_vm' THEN 'compute_shutdown'
                            WHEN 'deallocated_vm_residual_cost'
                                THEN 'compute_shutdown'
                            WHEN 'empty_paid_app_service_plan'
                                THEN 'empty_app_service_plan'
                            ELSE finding.rule_id
                        END AS family
                    FROM rule_opportunity_snapshots AS finding
                    JOIN valid
                      ON valid.snapshot_id = finding.snapshot_id
                     AND valid.source = 'FluxIntelligence'
                )
                SELECT source, snapshot_id, max(observed_at), resource_id, family
                FROM occurrence
                WHERE resource_id <> ''
                GROUP BY source, snapshot_id, resource_id, family
                """
            ).fetchall()
            current = db.execute(
                """
                WITH current_finding AS (
                    SELECT
                        advisor.resource_id,
                        CASE
                            WHEN lower(advisor.problem || ' ' || advisor.solution)
                                LIKE '%unattached%disk%' THEN 'unattached_disk'
                            WHEN lower(advisor.problem || ' ' || advisor.solution)
                                LIKE '%underutilized%virtual machine%'
                                THEN 'compute_shutdown'
                            WHEN lower(advisor.problem || ' ' || advisor.solution)
                                LIKE '%app service plan%'
                                THEN 'empty_app_service_plan'
                            ELSE 'advisor_' ||
                                lower(replace(advisor.category, ' ', '_'))
                        END AS family,
                        'azure_advisor' AS source,
                        0.9 AS evidence,
                        advisor.observed_at
                    FROM advisor_recommendations_current AS advisor
                    WHERE advisor.resource_id <> ''

                    UNION ALL

                    SELECT
                        finding.resource_id,
                        CASE finding.rule_id
                            WHEN 'stopped_allocated_vm' THEN 'compute_shutdown'
                            WHEN 'deallocated_vm_residual_cost'
                                THEN 'compute_shutdown'
                            WHEN 'empty_paid_app_service_plan'
                                THEN 'empty_app_service_plan'
                            ELSE finding.rule_id
                        END AS family,
                        finding.source,
                        CASE lower(finding.confidence)
                            WHEN 'high' THEN 1.0
                            WHEN 'medium' THEN 0.7
                            ELSE 0.4
                        END AS evidence,
                        finding.observed_at
                    FROM rule_opportunities_current AS finding
                    WHERE finding.resource_id <> ''
                )
                SELECT
                    finding.resource_id,
                    finding.family,
                    count(DISTINCT finding.source) AS source_count,
                    max(finding.evidence) AS source_evidence,
                    max(finding.observed_at) AS last_seen,
                    min(finding.observed_at) AS evidence_freshness_at,
                    COALESCE(attempt.status, '') AS telemetry_status
                FROM current_finding AS finding
                LEFT JOIN telemetry_resource_attempts_current AS attempt
                  ON attempt.resource_id = finding.resource_id
                 AND attempt.source = 'azure_monitor'
                GROUP BY
                    finding.resource_id,
                    finding.family,
                    attempt.status
                """
            ).fetchall()

        snapshot_index: dict[str, dict[str, int]] = {}
        for source, source_snapshot, _ in valid_snapshots:
            source_index = snapshot_index.setdefault(source, {})
            source_index[source_snapshot] = len(source_index)
        history: dict[tuple[str, str, str], dict[str, datetime]] = {}
        for (
            occurrence_source,
            occurrence_snapshot,
            observed_at,
            resource_id,
            family,
        ) in occurrences:
            history.setdefault((resource_id, family, occurrence_source), {})[
                occurrence_snapshot
            ] = observed_at

        rows: list[list[Any]] = []
        for (
            resource_id,
            family,
            source_count,
            source_evidence,
            current_last_seen,
            evidence_freshness_at,
            telemetry_status,
        ) in current:
            tracks = [
                (source, observed)
                for (item_resource, item_family, source), observed in history.items()
                if item_resource == resource_id and item_family == family
            ]
            if not tracks:
                first_seen = current_last_seen
                last_seen = current_last_seen
                consecutive_count = 1
                reappeared = False
            else:
                first_seen = min(
                    min(observed.values()) for _, observed in tracks
                )
                last_seen = max(
                    current_last_seen,
                    *(max(observed.values()) for _, observed in tracks),
                )
                consecutive_count = 1
                reappeared = False
                for source, observed in tracks:
                    index_by_snapshot = snapshot_index.get(source, {})
                    positions = sorted(
                        index_by_snapshot[item]
                        for item in observed
                        if item in index_by_snapshot
                    )
                    if not positions:
                        continue
                    current_consecutive = 1
                    for index in range(len(positions) - 2, -1, -1):
                        if positions[index] == positions[index + 1] - 1:
                            current_consecutive += 1
                        else:
                            break
                    consecutive_count = max(
                        consecutive_count, current_consecutive
                    )
                    seen_positions = set(positions)
                    reappeared = reappeared or any(
                        position not in seen_positions
                        for position in range(positions[0], positions[-1])
                    )

            score = confidence_score(
                family=family,
                consecutive_count=consecutive_count,
                source_count=source_count,
                source_evidence=source_evidence,
                last_seen=evidence_freshness_at,
                computed_at=computed_at,
                telemetry_status=telemetry_status,
            )
            factors = {
                **score,
                "sourceCount": source_count,
                "telemetryStatus": telemetry_status or "not_attempted",
                "ageDays": max(0, (computed_at - first_seen).days),
                "evidenceFreshnessAt": evidence_freshness_at.isoformat(),
            }
            rows.append(
                [
                    snapshot_id,
                    computed_at,
                    resource_id,
                    family,
                    first_seen,
                    last_seen,
                    consecutive_count,
                    reappeared,
                    score["score"],
                    score["label"],
                    json_value(factors),
                    METHOD_VERSION,
                ]
            )

        with self.connect() as db:
            db.execute(
                "DELETE FROM opportunity_confidence_snapshots WHERE snapshot_id = ?",
                [snapshot_id],
            )
            if rows:
                db.executemany(
                    """
                    INSERT INTO opportunity_confidence_snapshots VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    rows,
                )
        if rows:
            self._refresh_opportunity_scores("confidence")
        return len(rows)

    def ensure_opportunity_confidence(self) -> int:
        with self.connect(read_only=True) as db:
            latest_snapshot = db.execute(
                """
                SELECT arg_max(snapshot_id, observed_at)
                FROM resource_snapshots
                """
            ).fetchone()[0]
            existing = db.execute(
                """
                SELECT count(*)
                FROM opportunity_confidence_snapshots
                WHERE method_version = ?
                """,
                [METHOD_VERSION],
            ).fetchone()[0]
        if not latest_snapshot or existing:
            return 0
        return self.compute_opportunity_confidence(latest_snapshot)

    def retail_price_requests(
        self,
        *,
        refresh_hours: int = 24,
    ) -> list[dict[str, Any]]:
        cutoff = (
            utc_now() - timedelta(hours=refresh_hours)
            if refresh_hours > 0 else None
        )
        with self.connect(read_only=True) as db:
            rows = db.execute(
                """
                WITH cost_currency AS (
                    SELECT
                        resource_id,
                        arg_max(
                            currency,
                            CASE cost_type
                                WHEN 'AmortizedCost' THEN 2 ELSE 1
                            END
                        ) AS currency
                    FROM costs_current
                    WHERE resource_id <> ''
                    GROUP BY resource_id
                )
                , vm_prices AS (
                    SELECT lower(resource.resource_id) AS resource_id,
                           lower(resource.region) AS region,
                           resource.sku AS target_sku,
                           '' AS savings_currency,
                           resource.raw_json
                    FROM resources_current AS resource
                    WHERE lower(resource.resource_type) =
                          'microsoft.compute/virtualmachines'
                      AND resource.region <> '' AND resource.sku <> ''
                    UNION
                    SELECT advisor.resource_id,
                           lower(resource.region),
                           advisor.recommended_sku,
                           advisor.savings_currency,
                           resource.raw_json
                    FROM advisor_recommendations_current AS advisor
                    JOIN resources_current AS resource
                      ON lower(resource.resource_id) = advisor.resource_id
                    WHERE advisor.recommended_sku <> ''
                      AND resource.region <> ''
                      AND lower(COALESCE(advisor.resource_type, '')) =
                          'microsoft.compute/virtualmachines'
                )
                SELECT prices.region, prices.target_sku,
                       upper(COALESCE(
                           NULLIF(cost.currency, ''),
                           NULLIF(prices.savings_currency, ''),
                           'USD'
                       )) AS currency,
                       COALESCE(
                           json_extract_string(prices.raw_json, '$.osType'),
                           ''
                       ) AS os_type,
                       COALESCE(
                           json_extract_string(
                               prices.raw_json, '$.licenseType'
                           ),
                           ''
                       ) AS license_type
                FROM vm_prices AS prices
                LEFT JOIN cost_currency AS cost
                  ON cost.resource_id = prices.resource_id
                """
            ).fetchall()
            attempts = {
                (row[0], row[1], row[2], row[3]): row[4]
                for row in db.execute(
                    """
                    SELECT
                        arm_region_name, arm_sku_name,
                        price_profile, currency, observed_at
                    FROM retail_price_attempts_current
                    """
                ).fetchall()
            }

        requests: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for region, target_sku, currency, os_type, license_type in rows:
            profile, license_model = price_profile(os_type, license_type)
            key = (region, target_sku, profile, currency)
            last_attempt = attempts.get(key)
            if cutoff is not None and last_attempt and last_attempt >= cutoff:
                continue
            requests[key] = {
                "region": region,
                "targetSku": target_sku,
                "operatingSystem": str(os_type or "").lower(),
                "licenseModel": license_model,
                "priceProfile": profile,
                "currency": currency,
            }
        return [
            requests[key]
            for key in sorted(requests)
        ]

    def store_retail_prices(
        self,
        snapshot_id: str,
        prices: list[dict[str, Any]],
        *,
        complete: bool,
    ) -> int:
        observed_at = utc_now()
        rows = [
            [
                snapshot_id,
                observed_at,
                item["region"],
                item["targetSku"],
                item.get("operatingSystem", ""),
                item.get("licenseModel", ""),
                item.get("priceProfile", ""),
                item.get("currency", "USD").upper(),
                item["status"],
                item.get("hourlyPrice"),
                item.get("monthlyPrice"),
                item.get("monthlyComputePrice"),
                item.get("monthlyLicensePrice"),
                item.get("ri1YearMonthly"),
                item.get("ri1YearUpfront"),
                item.get("sp1YearMonthly"),
                item.get("hoursPerMonth", 730),
                item.get("meterId", ""),
                item.get("meterName", ""),
                item.get("productName", ""),
                item.get("skuName", ""),
                item.get("unitOfMeasure", ""),
                item.get("effectiveStartDate") or None,
                item.get("candidateCount", 0),
                item.get("source", "azure_retail_prices_api"),
                item.get("sourceUrl", ""),
                item.get("message", ""),
                json_value(item.get("raw", {})),
            ]
            for item in prices
        ]
        with self.connect() as db:
            db.execute("BEGIN TRANSACTION")
            try:
                if rows:
                    db.executemany(
                        """
                        INSERT INTO retail_price_snapshots (
                            snapshot_id, observed_at, arm_region_name,
                            arm_sku_name, operating_system, license_model,
                            price_profile, currency, status, hourly_price,
                            monthly_price, monthly_compute_price,
                            monthly_license_price, monthly_ri_1y,
                            ri_1y_upfront, monthly_sp_1y,
                            hours_per_month, meter_id,
                            meter_name, product_name, sku_name,
                            unit_of_measure, effective_start_date,
                            candidate_count, source, source_url, message,
                            raw_json
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        rows,
                    )
                if complete:
                    db.execute(
                        "INSERT INTO source_sync_state VALUES (?, ?, ?, ?, ?)",
                        [
                            snapshot_id,
                            observed_at,
                            "AzureRetailPrices",
                            "advisor-target-skus",
                            sum(
                                1 for item in prices
                                if item["status"] == "matched"
                            ),
                        ],
                    )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return len(rows)

    def compute_opportunity_valuation(self, snapshot_id: str) -> int:
        computed_at = utc_now()
        with self.connect(read_only=True) as db:
            candidates = db.execute(
                """
                WITH cost_by_type AS (
                    SELECT
                        resource_id,
                        cost_type,
                        sum(amount) AS amount,
                        max(currency) AS currency,
                        max(period_start) AS period_start,
                        max(period_end) AS period_end,
                        arg_max(snapshot_id, observed_at) AS cost_snapshot_id
                    FROM costs_current
                    WHERE resource_id <> ''
                    GROUP BY resource_id, cost_type
                ),
                resource_cost AS (
                    SELECT * EXCLUDE (cost_rank)
                    FROM (
                        SELECT *,
                            row_number() OVER (
                                PARTITION BY resource_id
                                ORDER BY CASE cost_type
                                    WHEN 'AmortizedCost' THEN 1 ELSE 2 END
                            ) AS cost_rank
                        FROM cost_by_type
                    )
                    WHERE cost_rank = 1
                ),
                opportunity AS (
                    SELECT
                        advisor.resource_id,
                        CASE
                            WHEN lower(advisor.problem || ' ' || advisor.solution)
                                LIKE '%unattached%disk%' THEN 'unattached_disk'
                            WHEN lower(advisor.problem || ' ' || advisor.solution)
                                LIKE '%underutilized%virtual machine%'
                                THEN 'compute_shutdown'
                            WHEN lower(advisor.problem || ' ' || advisor.solution)
                                LIKE '%app service plan%'
                                THEN 'empty_app_service_plan'
                            ELSE 'advisor_' ||
                                lower(replace(advisor.category, ' ', '_'))
                        END AS family,
                        'azure_advisor' AS source,
                        '' AS rule_id,
                        advisor.savings_amount,
                        advisor.annual_savings_amount,
                        advisor.savings_currency,
                        COALESCE(
                            NULLIF(advisor.current_sku, ''),
                            resource.sku,
                            ''
                        ) AS current_sku,
                        advisor.recommended_sku,
                        COALESCE(resource.region, '') AS region,
                        COALESCE(
                            json_extract_string(resource.raw_json, '$.osType'),
                            ''
                        ) AS os_type,
                        COALESCE(
                            json_extract_string(
                                resource.raw_json,
                                '$.licenseType'
                            ),
                            ''
                        ) AS license_type
                    FROM advisor_recommendations_current AS advisor
                    LEFT JOIN resources_current AS resource
                      ON lower(resource.resource_id) = advisor.resource_id

                    UNION ALL

                    SELECT
                        finding.resource_id,
                        CASE finding.rule_id
                            WHEN 'stopped_allocated_vm' THEN 'compute_shutdown'
                            WHEN 'deallocated_vm_residual_cost'
                                THEN 'compute_shutdown'
                            WHEN 'empty_paid_app_service_plan'
                                THEN 'empty_app_service_plan'
                            ELSE finding.rule_id
                        END AS family,
                        finding.source,
                        finding.rule_id,
                        finding.estimated_monthly_savings,
                        NULL::DOUBLE,
                        finding.savings_currency,
                        '',
                        '',
                        COALESCE(resource.region, ''),
                        '',
                        ''
                    FROM rule_opportunities_current AS finding
                    LEFT JOIN resources_current AS resource
                      ON lower(resource.resource_id) = finding.resource_id
                ),
                profiled AS (
                    SELECT opportunity.*,
                        CASE
                            WHEN lower(os_type) = 'linux' THEN 'linux'
                            WHEN lower(os_type) = 'windows'
                             AND lower(license_type) = 'windows_server'
                                THEN 'linux'
                            WHEN lower(os_type) = 'windows' THEN 'windows'
                            ELSE 'unknown'
                        END AS price_profile,
                        CASE
                            WHEN lower(os_type) = 'linux' THEN 'linux'
                            WHEN lower(os_type) = 'windows'
                             AND lower(license_type) = 'windows_server'
                                THEN 'azure_hybrid_benefit'
                            WHEN lower(os_type) = 'windows'
                                THEN 'license_included'
                            ELSE 'unknown'
                        END AS license_model
                    FROM opportunity
                )
                SELECT
                    profiled.resource_id,
                    profiled.family,
                    profiled.source,
                    profiled.rule_id,
                    profiled.savings_amount,
                    profiled.annual_savings_amount,
                    COALESCE(
                        NULLIF(cost.currency, ''),
                        NULLIF(profiled.savings_currency, ''),
                        ''
                    ) AS currency,
                    cost.amount,
                    COALESCE(cost.cost_type, ''),
                    cost.period_start,
                    cost.period_end,
                    COALESCE(cost.cost_snapshot_id, ''),
                    confidence.confidence,
                    profiled.current_sku,
                    profiled.recommended_sku,
                    profiled.region,
                    profiled.os_type,
                    profiled.license_type,
                    profiled.price_profile,
                    profiled.license_model,
                    COALESCE(
                        price.status,
                        price_attempt.status,
                        CASE
                            WHEN profiled.recommended_sku = ''
                                THEN 'missing_target_sku'
                            ELSE 'not_collected'
                        END
                    ) AS target_price_status,
                    COALESCE(price.snapshot_id, ''),
                    price.hourly_price,
                    price.monthly_price,
                    COALESCE(price.currency, ''),
                    price.hours_per_month,
                    COALESCE(price.meter_id, ''),
                    COALESCE(price.meter_name, ''),
                    COALESCE(price.product_name, ''),
                    price.effective_start_date,
                    price.observed_at
                FROM profiled
                LEFT JOIN resource_cost AS cost
                  ON cost.resource_id = profiled.resource_id
                LEFT JOIN opportunity_confidence_current AS confidence
                  ON confidence.resource_id = profiled.resource_id
                 AND confidence.opportunity_type = profiled.family
                LEFT JOIN retail_prices_current AS price
                  ON price.arm_region_name = lower(profiled.region)
                 AND price.arm_sku_name = profiled.recommended_sku
                 AND price.price_profile = profiled.price_profile
                 AND price.currency = upper(COALESCE(
                        NULLIF(cost.currency, ''),
                        NULLIF(profiled.savings_currency, ''),
                        'USD'
                    ))
                LEFT JOIN retail_price_attempts_current AS price_attempt
                  ON price_attempt.arm_region_name = lower(profiled.region)
                 AND price_attempt.arm_sku_name = profiled.recommended_sku
                 AND price_attempt.price_profile = profiled.price_profile
                 AND price_attempt.currency = upper(COALESCE(
                        NULLIF(cost.currency, ''),
                        NULLIF(profiled.savings_currency, ''),
                        'USD'
                    ))
                """
            ).fetchall()

        selected: dict[tuple[str, str], tuple[int, list[Any]]] = {}
        for row in candidates:
            result = value_opportunity(
                source=row[2],
                rule_id=row[3],
                advisor_monthly=row[4],
                advisor_annual=row[5],
                cost_amount=row[7],
                cost_type=row[8],
                period_start=row[9],
                period_end=row[10],
                confidence=row[12],
                current_sku=row[13],
                target_sku=row[14],
                target_price_status=row[20],
                target_monthly_price=row[23],
                target_price_currency=row[24],
                cost_currency=row[6],
            )
            valued = result["monthlyGross"] is not None
            priority = (
                0
                if valued and row[2] == "azure_advisor"
                else 1
                if valued
                else 2
                if row[2] == "azure_advisor"
                else 3
            )
            value_row = [
                snapshot_id,
                computed_at,
                row[0],
                row[1],
                row[2],
                result["status"],
                result["monthlyGross"],
                result["monthlyRiskAdjusted"],
                row[6],
                result["valueSource"],
                result["basis"],
                row[11],
                row[8],
                row[9],
                row[10],
                result["confidence"],
                VALUATION_METHOD_VERSION,
                result["currentMonthlyCost"],
                result["targetMonthlyCost"],
                (
                    f"{row[8]} amount normalized from {row[9]} through "
                    f"{row[10]} to a calendar-month run rate."
                    if row[7] is not None and row[9] and row[10]
                    else ""
                ),
                (
                    f"Microsoft retail rate {row[22]:.6f} {row[24]}/hour "
                    f"projected at {row[25]:g} hours/month; "
                    f"{row[28]} ({row[27]}), effective {row[29]}."
                    if row[20] == "matched" and row[22] is not None
                    else ""
                ),
                row[21],
                row[20],
                row[22],
                row[25],
                row[26],
                row[27],
                row[28],
                row[29],
                row[13],
                row[14],
                row[15],
                row[16],
                row[19],
            ]
            key = (row[0], row[1])
            if key not in selected or priority < selected[key][0]:
                selected[key] = (priority, value_row)

        rows = [item[1] for item in selected.values()]
        with self.connect() as db:
            db.execute(
                "DELETE FROM opportunity_valuation_snapshots_v2 WHERE snapshot_id = ?",
                [snapshot_id],
            )
            if rows:
                db.executemany(
                    """
                    INSERT INTO opportunity_valuation_snapshots_v2 (
                        snapshot_id, computed_at, resource_id,
                        opportunity_type, source, valuation_status,
                        monthly_gross, monthly_risk_adjusted, currency,
                        value_source, valuation_basis, cost_snapshot_id,
                        cost_type, cost_period_start, cost_period_end,
                        confidence, method_version, current_monthly_cost,
                        target_monthly_cost, current_cost_basis,
                        target_price_basis, target_price_snapshot_id,
                        target_price_status, target_hourly_price,
                        target_hours_per_month, target_meter_id,
                        target_meter_name, target_product_name,
                        target_price_effective_start, current_sku, target_sku,
                        price_region, operating_system, license_model
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    rows,
                )
        if rows:
            self._refresh_opportunity_scores("valuation")
        return len(rows)

    def ensure_opportunity_valuation(self) -> int:
        with self.connect(read_only=True) as db:
            latest_snapshot = db.execute(
                "SELECT arg_max(snapshot_id, observed_at) FROM resource_snapshots"
            ).fetchone()[0]
            existing = db.execute(
                """
                SELECT count(*)
                FROM opportunity_valuation_snapshots_v2
                WHERE method_version = ? AND snapshot_id = ?
                """,
                [VALUATION_METHOD_VERSION, latest_snapshot],
            ).fetchone()[0]
        if not latest_snapshot or existing:
            return 0
        return self.compute_opportunity_valuation(latest_snapshot)

    def latest_inventory_snapshot_id(self) -> str:
        with self.connect(read_only=True) as db:
            value = db.execute(
                """
                SELECT arg_max(snapshot_id, observed_at)
                FROM source_sync_state
                WHERE source = 'AzureResourceGraph'
                  AND scope_id = 'configured-subscriptions'
                """
            ).fetchone()[0]
        return str(value or "")

    def latest_telemetry_run_id(self) -> str:
        with self.connect(read_only=True) as db:
            value = db.execute(
                """
                SELECT arg_max(id, completed_at)
                FROM telemetry_runs
                WHERE source IN ('azure_monitor', 'logicmonitor')
                  AND status = 'succeeded'
                """
            ).fetchone()[0]
        return str(value or "")

    @staticmethod
    def _resource_map(rows: list[tuple[Any, ...]]) -> dict[str, dict[str, Any]]:
        values: dict[str, dict[str, Any]] = {}
        for row in rows:
            tags = row[10]
            if isinstance(tags, str):
                tags = json.loads(tags or "{}")
            resource_id = str(row[0]).lower()
            values[resource_id] = {
                "resourceId": resource_id,
                "name": row[1],
                "resourceType": row[2],
                "subscriptionId": row[3],
                "subscriptionName": row[4],
                "resourceGroup": row[5],
                "region": row[6],
                "kind": row[7],
                "sku": row[8],
                "managedBy": row[9],
                "tags": tags or {},
            }
        return values

    def compute_inventory_drift(
        self,
        snapshot_id: str,
        *,
        minimum_points: int = 5,
        threshold_k: float = 3.0,
    ) -> int:
        computed_at = utc_now()
        with self.connect(read_only=True) as db:
            snapshots = db.execute(
                """
                SELECT snapshot_id, max(observed_at) AS observed_at
                FROM resource_snapshots
                GROUP BY snapshot_id
                ORDER BY observed_at, snapshot_id
                """
            ).fetchall()
            positions = {
                item[0]: index for index, item in enumerate(snapshots)
            }
            position = positions.get(snapshot_id)
            if position is None or position == 0:
                return 0
            previous_snapshot_id = snapshots[position - 1][0]
            resource_rows = db.execute(
                """
                SELECT resource_id, name, resource_type, subscription_id,
                       subscription_name, resource_group, region, kind, sku,
                       managed_by, tags_json, snapshot_id
                FROM resource_snapshots
                WHERE snapshot_id IN (?, ?)
                """,
                [previous_snapshot_id, snapshot_id],
            ).fetchall()
            previous = self._resource_map(
                [row[:-1] for row in resource_rows if row[-1] == previous_snapshot_id]
            )
            current = self._resource_map(
                [row[:-1] for row in resource_rows if row[-1] == snapshot_id]
            )
            historical_runs = [
                row[0]
                for row in db.execute(
                    """
                    SELECT snapshot_id
                    FROM inventory_drift_runs
                    WHERE method_version = ? AND snapshot_id <> ?
                    ORDER BY computed_at
                    """,
                    [DRIFT_METHOD_VERSION, snapshot_id],
                ).fetchall()
            ]
            historical_counts = db.execute(
                """
                SELECT
                    snapshot_id,
                    'subscription' AS scope_type,
                    subscription_id AS scope_id,
                    subscription_id,
                    '' AS resource_group,
                    change_type,
                    count(*) AS change_count
                FROM inventory_changes
                WHERE method_version = ? AND snapshot_id <> ?
                GROUP BY snapshot_id, subscription_id, change_type

                UNION ALL

                SELECT
                    snapshot_id,
                    'resource_group' AS scope_type,
                    subscription_id || '/' || resource_group AS scope_id,
                    subscription_id,
                    resource_group,
                    change_type,
                    count(*) AS change_count
                FROM inventory_changes
                WHERE method_version = ? AND snapshot_id <> ?
                GROUP BY
                    snapshot_id, subscription_id, resource_group, change_type
                """,
                [
                    DRIFT_METHOD_VERSION,
                    snapshot_id,
                    DRIFT_METHOD_VERSION,
                    snapshot_id,
                ],
            ).fetchall()

        changes = classify_changes(previous, current)
        change_rows = [
            [
                snapshot_id,
                previous_snapshot_id,
                computed_at,
                item["resourceId"],
                item["resourceName"],
                item["resourceType"],
                item["subscriptionId"],
                item["subscriptionName"],
                item["resourceGroup"],
                item["region"],
                item["changeType"],
                item["fromFingerprint"],
                item["toFingerprint"],
                json_value(item["details"]),
                DRIFT_METHOD_VERSION,
            ]
            for item in changes
        ]
        history_lookup = {
            (row[0], row[1], row[2], row[5]): row[6]
            for row in historical_counts
        }
        anomaly_rows = []
        for (
            scope_type,
            scope_id,
            subscription_id,
            resource_group,
            change_type,
        ), count in change_counts(changes).items():
            history = [
                history_lookup.get(
                    (run_id, scope_type, scope_id, change_type),
                    0,
                )
                for run_id in historical_runs
            ]
            result = anomaly_result(
                count,
                history,
                minimum_points=minimum_points,
                threshold_k=threshold_k,
            )
            anomaly_rows.append(
                [
                    snapshot_id,
                    computed_at,
                    scope_type,
                    scope_id,
                    subscription_id,
                    resource_group,
                    change_type,
                    count,
                    result["baselinePoints"],
                    result["baselineMedian"],
                    result["mad"],
                    result["kScore"],
                    threshold_k,
                    result["status"],
                    DRIFT_METHOD_VERSION,
                ]
            )

        with self.connect() as db:
            db.execute("BEGIN TRANSACTION")
            try:
                db.execute(
                    "DELETE FROM inventory_changes WHERE snapshot_id = ?",
                    [snapshot_id],
                )
                db.execute(
                    "DELETE FROM inventory_change_anomalies WHERE snapshot_id = ?",
                    [snapshot_id],
                )
                db.execute(
                    "DELETE FROM inventory_drift_runs WHERE snapshot_id = ?",
                    [snapshot_id],
                )
                db.execute(
                    """
                    INSERT INTO inventory_drift_runs VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        snapshot_id,
                        previous_snapshot_id,
                        computed_at,
                        len(changes),
                        DRIFT_METHOD_VERSION,
                    ],
                )
                if change_rows:
                    db.executemany(
                        """
                        INSERT INTO inventory_changes VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        change_rows,
                    )
                if anomaly_rows:
                    db.executemany(
                        """
                        INSERT INTO inventory_change_anomalies VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        anomaly_rows,
                    )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return len(changes)

    def ensure_inventory_drift(
        self,
        *,
        minimum_points: int = 5,
        threshold_k: float = 3.0,
    ) -> int:
        with self.connect(read_only=True) as db:
            latest = db.execute(
                "SELECT arg_max(snapshot_id, observed_at) FROM resource_snapshots"
            ).fetchone()[0]
            existing = db.execute(
                """
                SELECT count(*) FROM inventory_drift_runs
                WHERE snapshot_id = ? AND method_version = ?
                """,
                [latest, DRIFT_METHOD_VERSION],
            ).fetchone()[0] if latest else 0
        if not latest or existing:
            return 0
        return self.compute_inventory_drift(
            latest,
            minimum_points=minimum_points,
            threshold_k=threshold_k,
        )

    def changes(
        self,
        *,
        search: str = "",
        change_type: str = "",
        subscription_id: str = "",
        resource_group: str = "",
        window_days: int = 0,
        limit: int = 250,
        offset: int = 0,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        params: list[Any] = []
        # window_days <= 0 means "all retained history"; the facet lists stay
        # unfiltered so the dropdowns do not collapse as the window narrows.
        if window_days > 0:
            conditions.append(
                "computed_at >= CURRENT_TIMESTAMP - (? * INTERVAL '1' DAY)"
            )
            params.append(int(window_days))
        if search:
            token = f"%{search}%"
            conditions.append(
                "(resource_name ILIKE ? OR resource_id ILIKE ? "
                "OR resource_type ILIKE ? OR resource_group ILIKE ?)"
            )
            params.extend([token, token, token, token])
        if change_type:
            conditions.append("change_type = ?")
            params.append(change_type)
        if subscription_id:
            conditions.append("subscription_id = ?")
            params.append(subscription_id)
        if resource_group:
            conditions.append("resource_group = ?")
            params.append(resource_group)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect(read_only=True) as db:
            total = db.execute(
                f"SELECT count(*) FROM inventory_changes_current {where}",
                params,
            ).fetchone()[0]
            rows = db.execute(
                f"""
                SELECT snapshot_id, previous_snapshot_id, computed_at,
                       resource_id, resource_name, resource_type,
                       subscription_id, subscription_name, resource_group,
                       region, change_type, details_json, method_version
                FROM inventory_changes_current
                {where}
                ORDER BY computed_at DESC, change_type, resource_name
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            facets = {
                "changeTypes": [
                    row[0]
                    for row in db.execute(
                        """
                        SELECT DISTINCT change_type
                        FROM inventory_changes_current ORDER BY 1
                        """
                    ).fetchall()
                ],
                "subscriptions": [
                    {"id": row[0], "name": row[1] or row[0]}
                    for row in db.execute(
                        """
                        SELECT DISTINCT subscription_id, subscription_name
                        FROM inventory_changes_current
                        WHERE subscription_id <> ''
                        ORDER BY 2, 1
                        """
                    ).fetchall()
                ],
                "resourceGroups": [
                    row[0]
                    for row in db.execute(
                        """
                        SELECT DISTINCT resource_group
                        FROM inventory_changes_current
                        WHERE resource_group <> ''
                        ORDER BY 1
                        """
                    ).fetchall()
                ],
            }
            summary = db.execute(
                """
                SELECT
                    count(*),
                    count(*) FILTER (WHERE change_type = 'created'),
                    count(*) FILTER (WHERE change_type = 'deleted'),
                    count(*) FILTER (
                        WHERE change_type NOT IN ('created', 'deleted')
                    )
                FROM inventory_changes_current
                """
            ).fetchone()
        labels = self.subscription_labels()
        return {
            "items": [
                {
                    "snapshotId": row[0],
                    "previousSnapshotId": row[1],
                    "computedAt": row[2].isoformat(),
                    "resourceId": row[3],
                    "resourceName": row[4],
                    "resourceType": row[5],
                    "subscriptionId": row[6],
                    "subscriptionName": (
                        row[7]
                        if row[7] and row[7] != row[6]
                        else labels.get(str(row[6] or "").lower())
                        or row[7]
                        or row[6]
                    ),
                    "resourceGroup": row[8],
                    "region": row[9],
                    "changeType": row[10],
                    "details": json.loads(row[11]) if row[11] else {},
                    "methodVersion": row[12],
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "facets": facets,
            "summary": {
                "total": summary[0],
                "created": summary[1],
                "deleted": summary[2],
                "configuration": summary[3],
            },
        }

    def change_anomalies(self) -> dict[str, Any]:
        with self.connect(read_only=True) as db:
            rows = db.execute(
                """
                SELECT snapshot_id, computed_at, scope_type, scope_id,
                       subscription_id, resource_group, change_type,
                       change_count, baseline_points, baseline_median, mad,
                       k_score, threshold_k, status, method_version
                FROM inventory_change_anomalies_current
                ORDER BY
                    CASE status WHEN 'anomalous' THEN 1
                        WHEN 'warming_up' THEN 2 ELSE 3 END,
                    change_count DESC, scope_type, scope_id
                """
            ).fetchall()
        return {
            "items": [
                {
                    "snapshotId": row[0],
                    "computedAt": row[1].isoformat(),
                    "scopeType": row[2],
                    "scopeId": row[3],
                    "subscriptionId": row[4],
                    "resourceGroup": row[5],
                    "changeType": row[6],
                    "changeCount": row[7],
                    "baselinePoints": row[8],
                    "baselineMedian": row[9],
                    "mad": row[10],
                    "kScore": row[11],
                    "thresholdK": row[12],
                    "status": row[13],
                    "methodVersion": row[14],
                }
                for row in rows
            ],
            "anomalyCount": sum(1 for row in rows if row[13] == "anomalous"),
            "warmingUp": bool(rows) and all(
                row[13] == "warming_up" for row in rows
            ),
        }

    def cost_anomalies(
        self,
        *,
        search: str = "",
        cost_type: str = "AmortizedCost",
        scope_type: str = "",
        subscription_id: str = "",
        service_name: str = "",
        severity: str = "",
        status: str = "anomalous",
        latency_days: int = 0,
        limit: int = 250,
        offset: int = 0,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        params: list[Any] = []
        if search:
            token = f"%{search}%"
            conditions.append(
                "(anomaly.resource_name ILIKE ? OR anomaly.resource_id ILIKE ? "
                "OR anomaly.resource_group ILIKE ? OR anomaly.service_name ILIKE ?)"
            )
            params.extend([token, token, token, token])
        if cost_type:
            conditions.append("anomaly.cost_type = ?")
            params.append(cost_type)
        if scope_type:
            conditions.append("anomaly.scope_type = ?")
            params.append(scope_type)
        if subscription_id:
            conditions.append("anomaly.subscription_id = ?")
            params.append(subscription_id.lower())
        if service_name:
            conditions.append("anomaly.service_name = ?")
            params.append(service_name)
        if severity:
            conditions.append("anomaly.severity = ?")
            params.append(severity)
        summary_conditions = list(conditions)
        summary_params = list(params)
        if status:
            conditions.append("anomaly.status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        summary_where = (
            f"WHERE {' AND '.join(summary_conditions)}"
            if summary_conditions
            else ""
        )
        with self.connect(read_only=True) as db:
            latest_run = db.execute(
                """
                SELECT run_id, evaluated_at, evaluation_date, evaluated_count,
                       anomaly_count, warming_count, message, method_version
                FROM cost_anomaly_runs
                WHERE status = 'succeeded'
                ORDER BY evaluated_at DESC LIMIT 1
                """
            ).fetchone()
            total = db.execute(
                f"""
                SELECT count(*)
                FROM cost_anomalies_current AS anomaly
                {where}
                """,
                params,
            ).fetchone()[0]
            rows = db.execute(
                f"""
                SELECT anomaly.run_id, anomaly.evaluated_at,
                       anomaly.evaluation_date, anomaly.cost_type,
                       anomaly.scope_type, anomaly.scope_id,
                       anomaly.subscription_id, anomaly.resource_id,
                       anomaly.resource_name, anomaly.resource_type,
                       anomaly.resource_group, anomaly.service_name,
                       anomaly.current_amount, anomaly.baseline_points,
                       anomaly.baseline_median, anomaly.mad, anomaly.k_score,
                       anomaly.previous_week_amount, anomaly.absolute_change,
                       anomaly.percent_change, anomaly.status, anomaly.severity,
                       anomaly.currency, anomaly.reason, anomaly.method_version,
                       COALESCE(review.review_status, 'new'),
                       COALESCE(review.note, ''), review.updated_by,
                       review.updated_at
                FROM cost_anomalies_current AS anomaly
                LEFT JOIN cost_anomaly_reviews AS review
                  ON review.run_id = anomaly.run_id
                 AND review.cost_type = anomaly.cost_type
                 AND review.scope_type = anomaly.scope_type
                 AND review.scope_id = anomaly.scope_id
                {where}
                ORDER BY
                    CASE anomaly.severity WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2 ELSE 3 END,
                    anomaly.absolute_change DESC NULLS LAST,
                    anomaly.resource_name
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            summary = db.execute(
                f"""
                SELECT count(*) FILTER (WHERE status = 'anomalous'),
                       count(*) FILTER (WHERE status = 'warming_up'),
                       COALESCE(sum(absolute_change) FILTER (
                           WHERE status = 'anomalous'
                       ), 0),
                       count(DISTINCT NULLIF(currency, '')),
                       any_value(NULLIF(currency, ''))
                FROM cost_anomalies_current AS anomaly
                {summary_where}
                """,
                summary_params,
            ).fetchone()
            facets = {
                "costTypes": [
                    item[0]
                    for item in db.execute(
                        """
                        SELECT DISTINCT cost_type
                        FROM cost_anomalies_current ORDER BY cost_type
                        """
                    ).fetchall()
                ],
                "scopeTypes": [
                    item[0]
                    for item in db.execute(
                        """
                        SELECT DISTINCT scope_type
                        FROM cost_anomalies_current ORDER BY scope_type
                        """
                    ).fetchall()
                ],
                "subscriptions": [
                    {"id": item[0], "name": item[1] or item[0]}
                    for item in db.execute(
                        """
                        WITH names AS (
                            SELECT subscription_id,
                                   any_value(NULLIF(subscription_name, '')) AS name
                            FROM resources_current GROUP BY subscription_id
                        )
                        SELECT DISTINCT anomaly.subscription_id,
                               names.name
                        FROM cost_anomalies_current AS anomaly
                        LEFT JOIN names USING (subscription_id)
                        ORDER BY names.name, anomaly.subscription_id
                        """
                    ).fetchall()
                ],
                "services": [
                    item[0]
                    for item in db.execute(
                        """
                        SELECT DISTINCT service_name
                        FROM cost_anomalies_current
                        WHERE service_name <> ''
                        ORDER BY service_name
                        """
                    ).fetchall()
                ],
                "severities": [
                    item[0]
                    for item in db.execute(
                        """
                        SELECT DISTINCT severity
                        FROM cost_anomalies_current
                        WHERE severity <> 'none'
                        ORDER BY severity
                        """
                    ).fetchall()
                ],
            }
            # Cost Management finalizes a usage day well after it ends, so the
            # trailing days are always partial and render as a false cliff.
            # Cut the series at the same latency the anomaly detector already
            # honours (FLUX_COST_ANOMALY_LATENCY_DAYS, default 2) rather than
            # charting known-incomplete days as if they were real drops.
            trend_rows = db.execute(
                """
                SELECT usage_date, sum(amount)
                FROM daily_cost_history
                WHERE cost_type = ?
                  AND usage_date >= current_date - INTERVAL 60 DAY
                  AND usage_date <= current_date - (? * INTERVAL '1' DAY)
                GROUP BY usage_date
                ORDER BY usage_date
                """,
                [cost_type or "AmortizedCost", max(0, int(latency_days))],
            ).fetchall()
            # Per-row sparklines: one batched query per scope type covering
            # the returned page, so each anomaly row carries its recent
            # daily spend without a per-row round trip.
            sparkline_map: dict[tuple[str, str], list[float]] = {}
            scope_column = {
                "subscription": "subscription_id",
                "service": "service_name",
                "resource": "resource_id",
            }
            scope_sets: dict[str, set[str]] = {key: set() for key in scope_column}
            for row in rows:
                if row[4] in scope_sets:
                    scope_sets[row[4]].add(str(row[5]))
            for scope_key, ids in scope_sets.items():
                if not ids:
                    continue
                placeholders = ", ".join("?" for _ in ids)
                column = scope_column[scope_key]
                series = db.execute(
                    f"""
                    SELECT {column}, usage_date, SUM(amount)
                    FROM daily_cost_history
                    WHERE cost_type = ?
                      AND usage_date >= current_date - INTERVAL 16 DAY
                      AND usage_date <= current_date - (? * INTERVAL '1' DAY)
                      AND {column} IN ({placeholders})
                    GROUP BY {column}, usage_date
                    ORDER BY usage_date
                    """,
                    [
                        cost_type or "AmortizedCost",
                        max(0, int(latency_days)),
                        *sorted(ids),
                    ],
                ).fetchall()
                for scope_value, _, amount in series:
                    sparkline_map.setdefault(
                        (scope_key, str(scope_value)), []
                    ).append(round(float(amount or 0), 2))
        currency = (
            "Mixed"
            if (summary[3] or 0) > 1
            else (summary[4] or "")
        )
        return {
            "items": [
                {
                    "runId": row[0],
                    "evaluatedAt": row[1].isoformat(),
                    "evaluationDate": row[2].isoformat(),
                    "costType": row[3],
                    "scopeType": row[4],
                    "scopeId": row[5],
                    "subscriptionId": row[6],
                    "resourceId": row[7],
                    "resourceName": row[8],
                    "resourceType": row[9],
                    "resourceGroup": row[10],
                    "serviceName": row[11],
                    "currentAmount": row[12],
                    "baselinePoints": row[13],
                    "baselineMedian": row[14],
                    "mad": row[15],
                    "kScore": row[16],
                    "previousWeekAmount": row[17],
                    "absoluteChange": row[18],
                    "percentChange": row[19],
                    "status": row[20],
                    "severity": row[21],
                    "currency": row[22],
                    "reason": row[23],
                    "methodVersion": row[24],
                    "reviewStatus": row[25],
                    "reviewNote": row[26],
                    "reviewedBy": row[27] or "",
                    "reviewedAt": row[28].isoformat() if row[28] else None,
                    "recentDailyAmounts": sparkline_map.get(
                        (row[4], str(row[5])), []
                    )[-14:],
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "facets": facets,
            "summary": {
                "anomalyCount": summary[0] or 0,
                "warmingCount": summary[1] or 0,
                "totalIncrease": round(summary[2] or 0, 2),
                "currency": currency,
                "evaluatedCount": latest_run[3] if latest_run else 0,
                "evaluationDate": (
                    latest_run[2].isoformat()
                    if latest_run and latest_run[2]
                    else None
                ),
                "evaluatedAt": (
                    latest_run[1].isoformat() if latest_run else None
                ),
                "methodVersion": latest_run[7] if latest_run else "",
                "message": latest_run[6] if latest_run else "",
                # The evaluator treats missing ingestion days as zero spend,
                # so holes in the baseline window depress medians and can
                # both mask real anomalies and flag phantom ones when a hole
                # ends. Disclosing baseline coverage lets the UI caveat the
                # results instead of presenting distorted math as certainty.
                "baselineCoverage": self._anomaly_baseline_coverage(
                    cost_type or "AmortizedCost"
                ),
            },
            "trend": [
                {"date": row[0].isoformat(), "amount": round(row[1] or 0, 2)}
                for row in trend_rows
            ],
            # Surfaced so the chart can state its cutoff instead of silently
            # ending early, which would read as a genuine spend drop.
            "trendLatencyDays": max(0, int(latency_days)),
        }

    def _anomaly_baseline_coverage(
        self,
        cost_type: str,
        *,
        baseline_weeks: int = 8,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """Estate ingestion coverage across the anomaly baseline window."""
        today = as_of or utc_now().date()
        window_end = today - timedelta(days=2)
        window_start = window_end - timedelta(days=baseline_weeks * 7)
        integration = self.integration()
        scope_ids = [
            item["subscriptionId"].lower()
            for item in integration.get("subscriptions", [])
            if item.get("subscriptionId")
        ]
        window_days = max((window_end - window_start).days + 1, 0)
        expected = window_days * len(scope_ids)
        if not expected:
            return {
                "windowStart": window_start.isoformat(),
                "windowEnd": window_end.isoformat(),
                "expectedScopeDays": 0,
                "ingestedScopeDays": 0,
                "coveragePercent": None,
            }
        placeholders = ", ".join("?" for _ in scope_ids)
        with self.connect(read_only=True) as db:
            ingested = db.execute(
                f"""
                SELECT count(*) FROM (
                    SELECT DISTINCT subscription_id, usage_date
                    FROM daily_cost_history
                    WHERE cost_type = ?
                      AND usage_date BETWEEN ? AND ?
                      AND subscription_id IN ({placeholders})
                )
                """,
                [cost_type, window_start, window_end, *scope_ids],
            ).fetchone()[0]
        ingested = int(ingested or 0)
        return {
            "windowStart": window_start.isoformat(),
            "windowEnd": window_end.isoformat(),
            "expectedScopeDays": expected,
            "ingestedScopeDays": ingested,
            "coveragePercent": round(ingested / expected * 100, 1),
        }

    def review_cost_anomaly(
        self,
        *,
        run_id: str,
        cost_type: str,
        scope_type: str,
        scope_id: str,
        review_status: str,
        note: str,
        updated_by: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect(read_only=True) as db:
            exists = db.execute(
                """
                SELECT 1
                FROM cost_anomaly_snapshots
                WHERE run_id = ? AND cost_type = ?
                  AND scope_type = ? AND scope_id = ?
                LIMIT 1
                """,
                [run_id, cost_type, scope_type, scope_id],
            ).fetchone()
        if not exists:
            raise ValueError("The selected cost anomaly no longer exists.")
        with self.operational_connect() as db:
            db.execute(
                """
                INSERT INTO cost_anomaly_reviews (
                    run_id, cost_type, scope_type, scope_id, review_status,
                    note, updated_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id, cost_type, scope_type, scope_id)
                DO UPDATE SET
                    review_status = excluded.review_status,
                    note = excluded.note,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                [
                    run_id, cost_type, scope_type, scope_id, review_status,
                    note.strip(), updated_by, now,
                ],
            )
        return {
            "runId": run_id,
            "costType": cost_type,
            "scopeType": scope_type,
            "scopeId": scope_id,
            "reviewStatus": review_status,
            "note": note.strip(),
            "updatedBy": updated_by,
            "updatedAt": now.isoformat(),
        }

    def cost_anomaly_contributors(
        self,
        *,
        run_id: str,
        cost_type: str,
        scope_type: str,
        scope_id: str,
    ) -> list[dict[str, Any]]:
        with self.connect(read_only=True) as db:
            anomaly = db.execute(
                """
                SELECT evaluation_date, subscription_id, service_name
                FROM cost_anomaly_snapshots
                WHERE run_id = ? AND cost_type = ?
                  AND scope_type = ? AND scope_id = ?
                LIMIT 1
                """,
                [run_id, cost_type, scope_type, scope_id],
            ).fetchone()
            if not anomaly:
                return []
            evaluation_date, subscription_id, service_name = anomaly
            previous_date = evaluation_date - timedelta(days=7)
            conditions = [
                "cost.cost_type = ?",
                "cost.subscription_id = ?",
                "cost.usage_date IN (?, ?)",
            ]
            params: list[Any] = [
                cost_type,
                subscription_id,
                evaluation_date,
                previous_date,
            ]
            if scope_type == "service" and service_name:
                conditions.append("cost.service_name = ?")
                params.append(service_name)
            if scope_type == "resource":
                conditions.append("cost.resource_id = ?")
                params.append(scope_id.lower())
            if scope_type == "subscription":
                key_sql = "COALESCE(NULLIF(cost.service_name, ''), 'Unallocated')"
                label_sql = key_sql
                contributor_type = "service"
            else:
                key_sql = "COALESCE(NULLIF(cost.resource_id, ''), cost.service_name)"
                label_sql = (
                    "COALESCE(NULLIF(resource.name, ''), "
                    "NULLIF(cost.resource_id, ''), cost.service_name)"
                )
                contributor_type = "resource"
            rows = db.execute(
                f"""
                SELECT {key_sql}, {label_sql},
                       COALESCE(sum(cost.amount) FILTER (
                           WHERE cost.usage_date = ?
                       ), 0),
                       COALESCE(sum(cost.amount) FILTER (
                           WHERE cost.usage_date = ?
                       ), 0),
                       any_value(cost.currency)
                FROM daily_cost_history AS cost
                LEFT JOIN resources_current AS resource
                  ON lower(resource.resource_id) = cost.resource_id
                WHERE {' AND '.join(conditions)}
                GROUP BY 1, 2
                """,
                [evaluation_date, previous_date, *params],
            ).fetchall()
        values = [
            {
                "type": contributor_type,
                "id": row[0],
                "name": row[1] or row[0],
                "current": round(float(row[2] or 0), 2),
                "previous": round(float(row[3] or 0), 2),
                "change": round(float(row[2] or 0) - float(row[3] or 0), 2),
                "currency": row[4] or "",
            }
            for row in rows
        ]
        return sorted(
            values,
            key=lambda item: abs(item["change"]),
            reverse=True,
        )[:10]

    def policy_report(
        self,
        *,
        subscription_id: str = "",
        assignment_id: str = "",
        compliance_state: str = "",
    ) -> dict[str, Any]:
        conditions = ["1 = 1"]
        params: list[Any] = []
        if subscription_id:
            conditions.append("subscription_id = ?")
            params.append(subscription_id.lower())
        if assignment_id:
            conditions.append("assignment_id = ?")
            params.append(assignment_id.lower())
        where = " AND ".join(conditions)
        with self.connect(read_only=True) as db:
            summary = db.execute(
                f"""
                SELECT COALESCE(sum(evaluated_count), 0),
                       COALESCE(sum(compliant_count), 0),
                       COALESCE(sum(non_compliant_count), 0),
                       COALESCE(sum(exempt_count), 0),
                       COALESCE(sum(unknown_count), 0),
                       count(*), max(observed_at)
                FROM policy_posture_current
                WHERE {where}
                """,
                params,
            ).fetchone()
            subscriptions = db.execute(
                f"""
                SELECT subscription_id,
                       any_value(subscription_name),
                       sum(evaluated_count), sum(compliant_count),
                       sum(non_compliant_count), sum(exempt_count),
                       count(*)
                FROM policy_posture_current
                WHERE {where}
                GROUP BY subscription_id
                ORDER BY sum(non_compliant_count) DESC, subscription_id
                """,
                params,
            ).fetchall()
            assignments = db.execute(
                f"""
                SELECT subscription_id, subscription_name, assignment_id,
                       assignment_name, evaluated_count, compliant_count,
                       non_compliant_count, exempt_count, unknown_count,
                       resource_count, definition_count
                FROM policy_posture_current
                WHERE {where}
                ORDER BY non_compliant_count DESC, assignment_name
                LIMIT 500
                """,
                params,
            ).fetchall()
            resource_conditions = list(conditions)
            resource_params = list(params)
            if compliance_state:
                resource_conditions.append("compliance_state = ?")
                resource_params.append(compliance_state)
            policy_resources = db.execute(
                f"""
                SELECT subscription_id, subscription_name, assignment_id,
                       assignment_name, definition_id, definition_name,
                       compliance_state, resource_id, resource_name,
                       resource_type, region, exemption_id, evaluated_at
                FROM policy_resources_current
                WHERE {' AND '.join(resource_conditions)}
                ORDER BY compliance_state DESC, assignment_name, resource_name
                LIMIT 5000
                """,
                resource_params,
            ).fetchall()
        evaluated = int(summary[0] or 0)
        compliant = int(summary[1] or 0)
        return {
            "summary": {
                "evaluated": evaluated,
                "compliant": compliant,
                "nonCompliant": int(summary[2] or 0),
                "exempt": int(summary[3] or 0),
                "unknown": int(summary[4] or 0),
                "assignmentCount": int(summary[5] or 0),
                "compliancePercent": round(compliant / evaluated * 100, 1)
                if evaluated
                else None,
                "observedAt": summary[6].isoformat() if summary[6] else None,
            },
            "bySubscription": [
                {
                    "id": row[0],
                    "name": row[1] or row[0],
                    "evaluated": row[2],
                    "compliant": row[3],
                    "nonCompliant": row[4],
                    "exempt": row[5],
                    "assignmentCount": row[6],
                    "compliancePercent": round(row[3] / row[2] * 100, 1)
                    if row[2]
                    else None,
                }
                for row in subscriptions
            ],
            "assignments": [
                {
                    "subscriptionId": row[0],
                    "subscriptionName": row[1],
                    "assignmentId": row[2],
                    "assignmentName": row[3],
                    "evaluated": row[4],
                    "compliant": row[5],
                    "nonCompliant": row[6],
                    "exempt": row[7],
                    "unknown": row[8],
                    "resourceCount": row[9],
                    "definitionCount": row[10],
                }
                for row in assignments
            ],
            "resources": [
                {
                    "subscriptionId": row[0],
                    "subscriptionName": row[1],
                    "assignmentId": row[2],
                    "assignmentName": row[3],
                    "definitionId": row[4],
                    "definitionName": row[5],
                    "complianceState": row[6],
                    "resourceId": row[7],
                    "resourceName": row[8],
                    "resourceType": row[9],
                    "region": row[10],
                    "exemptionId": row[11],
                    "evaluatedAt": row[12],
                }
                for row in policy_resources
            ],
            "lineage": {
                "source": "Azure Resource Graph PolicyResources",
                "scope": "Configured subscriptions",
                "limitation": (
                    "Counts and resource drilldown reflect the latest policy "
                    "states available in ARG; Flux remains read-only."
                ),
            },
        }

    def set_opportunity_lifecycle(
        self,
        opportunity_id: str,
        status: str,
        note: str = "",
        updated_by: str = "",
        resource_id: str = "",
        estimated_monthly_savings: float | None = None,
    ) -> dict[str, Any]:
        """Record a recommendation's lifecycle state.

        Implementation captures a baseline: the resource's current actual
        monthly run-rate at the moment of implementation, so realized savings
        can later be measured as baseline minus current spend rather than
        trusted from the original estimate.
        """
        now = utc_now()
        baseline = None
        implemented_at = None
        if status == "implemented":
            implemented_at = now
            if resource_id:
                with self.connect(read_only=True) as db:
                    row = db.execute(
                        """
                        SELECT SUM(CASE WHEN cost_type = 'ActualCost'
                            THEN amount END)
                        FROM costs_current
                        WHERE lower(resource_id) = lower(?)
                        """,
                        [resource_id],
                    ).fetchone()
                baseline = float(row[0]) if row and row[0] is not None else None
        with self.operational_connect() as db:
            db.execute(
                "DELETE FROM opportunity_lifecycle WHERE opportunity_id = ?",
                [opportunity_id],
            )
            db.execute(
                """
                INSERT INTO opportunity_lifecycle (
                    opportunity_id, status, note, updated_by, updated_at,
                    implemented_at, resource_id, estimated_monthly_savings,
                    baseline_monthly_cost
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    opportunity_id, status, note, updated_by, now,
                    implemented_at, resource_id, estimated_monthly_savings,
                    baseline,
                ],
            )
            db.commit()
        return {
            "opportunityId": opportunity_id,
            "status": status,
            "baselineMonthlyCost": baseline,
        }

    def opportunity_lifecycles(self) -> dict[str, str]:
        """Lifecycle status per opportunity id for list annotation."""
        with self.operational_connect(read_only=True) as db:
            rows = db.execute(
                "SELECT opportunity_id, status FROM opportunity_lifecycle"
            ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def commitment_inventory(self) -> dict[str, Any]:
        """Active reservations with utilization and time to expiry.

        The commitments collector has stored this tenant-wide inventory
        daily since it shipped; this is the read model that finally
        surfaces it. Non-Succeeded reservations (expired, cancelled) are
        counted but not listed: they are history, not posture.
        """
        today = date.today()
        with self.connect(read_only=True) as db:
            rows = db.execute(
                """
                SELECT reservation_id, display_name, sku, resource_type,
                       region, quantity, term, scope_type, state,
                       expiry_date, utilization_1d, utilization_7d,
                       utilization_30d
                FROM reservation_inventory_current
                ORDER BY expiry_date NULLS LAST, display_name
                """
            ).fetchall()
        reservations: list[dict[str, Any]] = []
        historical = 0
        for row in rows:
            state = str(row[8] or "")
            if state.lower() != "succeeded":
                historical += 1
                continue
            expiry = row[9]
            days_to_expiry = (expiry - today).days if expiry else None
            reservations.append(
                {
                    "reservationId": str(row[0] or ""),
                    "name": str(row[1] or ""),
                    "sku": str(row[2] or ""),
                    "resourceType": str(row[3] or ""),
                    "region": str(row[4] or ""),
                    "quantity": int(row[5] or 0),
                    "term": str(row[6] or ""),
                    "scopeType": str(row[7] or ""),
                    "expiryDate": expiry.isoformat() if expiry else None,
                    "daysToExpiry": days_to_expiry,
                    "utilization1d": (
                        float(row[10]) if row[10] is not None else None
                    ),
                    "utilization7d": (
                        float(row[11]) if row[11] is not None else None
                    ),
                    "utilization30d": (
                        float(row[12]) if row[12] is not None else None
                    ),
                }
            )
        total_quantity = sum(item["quantity"] for item in reservations)
        weighted_utilization = [
            (item["utilization30d"], item["quantity"])
            for item in reservations
            if item["utilization30d"] is not None and item["quantity"]
        ]
        weight = sum(quantity for _, quantity in weighted_utilization)
        return {
            "asOf": today.isoformat(),
            "summary": {
                "activeCount": len(reservations),
                "totalQuantity": total_quantity,
                "historicalCount": historical,
                "expiringWithin120Days": len(
                    [
                        item
                        for item in reservations
                        if item["daysToExpiry"] is not None
                        and item["daysToExpiry"] <= 120
                    ]
                ),
                "expiringWithin30Days": len(
                    [
                        item
                        for item in reservations
                        if item["daysToExpiry"] is not None
                        and item["daysToExpiry"] <= 30
                    ]
                ),
                "averageUtilization30d": (
                    round(
                        sum(
                            utilization * quantity
                            for utilization, quantity in weighted_utilization
                        )
                        / weight,
                        1,
                    )
                    if weight
                    else None
                ),
            },
            "reservations": reservations,
        }

    def savings_report(self) -> dict[str, Any]:
        """The savings funnel: estimated pipeline vs measured realization.

        Realized savings for implemented recommendations are measured from
        cost data — the captured implementation baseline minus the
        resource's current actual run-rate, floored at zero — never assumed
        from the original estimate.
        """
        with self.operational_connect(read_only=True) as db:
            rows = db.execute(
                """
                SELECT opportunity_id, status, note, updated_by, updated_at,
                       implemented_at, resource_id,
                       estimated_monthly_savings, baseline_monthly_cost
                FROM opportunity_lifecycle
                ORDER BY updated_at DESC
                """
            ).fetchall()
        resource_ids = {
            str(row[6]).lower() for row in rows if row[6] and row[1] == "implemented"
        }
        current_costs: dict[str, float] = {}
        if resource_ids:
            placeholders = ", ".join("?" for _ in resource_ids)
            with self.connect(read_only=True) as db:
                for resource_id, amount in db.execute(
                    f"""
                    SELECT lower(resource_id),
                           SUM(CASE WHEN cost_type = 'ActualCost'
                               THEN amount END)
                    FROM costs_current
                    WHERE lower(resource_id) IN ({placeholders})
                    GROUP BY lower(resource_id)
                    """,
                    sorted(resource_ids),
                ).fetchall():
                    if amount is not None:
                        current_costs[str(resource_id)] = float(amount)
        items = []
        totals = {
            "accepted": 0, "implemented": 0, "dismissed": 0,
            "estimatedAccepted": 0.0, "estimatedImplemented": 0.0,
            "realizedMonthly": 0.0, "measuredCount": 0,
        }
        for row in rows:
            status = str(row[1])
            estimated = float(row[7]) if row[7] is not None else None
            baseline = float(row[8]) if row[8] is not None else None
            current = current_costs.get(str(row[6]).lower()) if row[6] else None
            realized = None
            if status == "implemented" and baseline is not None and current is not None:
                realized = round(max(0.0, baseline - current), 2)
                totals["realizedMonthly"] += realized
                totals["measuredCount"] += 1
            if status in ("accepted", "implemented", "dismissed"):
                totals[status] += 1
            if status == "accepted" and estimated:
                totals["estimatedAccepted"] += estimated
            if status == "implemented" and estimated:
                totals["estimatedImplemented"] += estimated
            items.append(
                {
                    "opportunityId": str(row[0]),
                    "status": status,
                    "note": str(row[2] or ""),
                    "updatedBy": str(row[3] or ""),
                    "updatedAt": row[4].isoformat() if row[4] else None,
                    "implementedAt": row[5].isoformat() if row[5] else None,
                    "resourceId": str(row[6] or ""),
                    "estimatedMonthlySavings": estimated,
                    "baselineMonthlyCost": baseline,
                    "currentMonthlyCost": (
                        round(current, 2) if current is not None else None
                    ),
                    "realizedMonthlySavings": realized,
                }
            )
        return {
            "summary": {
                "acceptedCount": totals["accepted"],
                "implementedCount": totals["implemented"],
                "dismissedCount": totals["dismissed"],
                "estimatedAcceptedMonthly": round(totals["estimatedAccepted"], 2),
                "estimatedImplementedMonthly": round(
                    totals["estimatedImplemented"], 2
                ),
                "realizedMonthly": round(totals["realizedMonthly"], 2),
                "measuredCount": totals["measuredCount"],
            },
            "items": items,
        }

    def focus_analytics_report(self, window_days: int = 30) -> dict[str, Any]:
        """Charge-level commitment utilization and pricing analysis (FOCUS).

        Coverage and utilization come from CommitmentDiscount columns;
        Used/Unused state is read from the retained raw FOCUS payload since
        the normalized schema predates CommitmentDiscountStatus. Pricing
        analysis compares list, contracted, effective, and billed cost to
        show realized discounts by service.
        """
        with self.connect(read_only=True) as db:
            available = db.execute(
                "SELECT count(*) FROM focus_cost_charges"
            ).fetchone()
            if not available or not available[0]:
                return {"available": False}
            period = db.execute(
                """
                SELECT max(charge_period_start) - INTERVAL {days} DAY,
                       max(charge_period_start)
                FROM focus_cost_charges
                """.format(days=int(window_days))
            ).fetchone()
            coverage = db.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN commitment_discount_id <> ''
                        THEN effective_cost END), 0) AS committed,
                    COALESCE(SUM(CASE WHEN commitment_discount_id = ''
                        THEN effective_cost END), 0) AS on_demand,
                    any_value(billing_currency)
                FROM focus_cost_charges
                WHERE charge_category = 'Usage'
                  AND charge_period_start >= ?
                """,
                [period[0]],
            ).fetchone()
            commitments = db.execute(
                """
                SELECT
                    commitment_discount_id,
                    any_value(commitment_discount_name),
                    any_value(commitment_discount_type),
                    COALESCE(SUM(CASE WHEN COALESCE(
                            json_extract_string(raw_json, '$.CommitmentDiscountStatus'),
                            'Used') <> 'Unused'
                        THEN effective_cost END), 0) AS used_cost,
                    COALESCE(SUM(CASE WHEN json_extract_string(
                            raw_json, '$.CommitmentDiscountStatus') = 'Unused'
                        THEN effective_cost END), 0) AS unused_cost
                FROM focus_cost_charges
                WHERE commitment_discount_id <> ''
                  AND charge_period_start >= ?
                GROUP BY commitment_discount_id
                ORDER BY used_cost + unused_cost DESC
                LIMIT 50
                """,
                [period[0]],
            ).fetchall()
            pricing_totals = db.execute(
                """
                SELECT
                    COALESCE(SUM(billed_cost), 0),
                    COALESCE(SUM(effective_cost), 0),
                    COALESCE(SUM(list_cost), 0),
                    COALESCE(SUM(contracted_cost), 0)
                FROM focus_cost_charges
                WHERE charge_period_start >= ?
                """,
                [period[0]],
            ).fetchone()
            by_service = db.execute(
                """
                SELECT
                    service_name,
                    COALESCE(SUM(effective_cost), 0) AS effective,
                    COALESCE(SUM(list_cost), 0) AS list_cost,
                    COALESCE(SUM(billed_cost), 0) AS billed
                FROM focus_cost_charges
                WHERE charge_period_start >= ?
                GROUP BY service_name
                ORDER BY effective DESC
                LIMIT 12
                """,
                [period[0]],
            ).fetchall()
            by_pricing = db.execute(
                """
                SELECT pricing_category, COALESCE(SUM(effective_cost), 0)
                FROM focus_cost_charges
                WHERE charge_period_start >= ?
                GROUP BY pricing_category
                ORDER BY 2 DESC
                """,
                [period[0]],
            ).fetchall()

        committed, on_demand = float(coverage[0]), float(coverage[1])
        billed, effective, list_cost, contracted = (
            float(pricing_totals[0]), float(pricing_totals[1]),
            float(pricing_totals[2]), float(pricing_totals[3]),
        )

        def pct(part: float, whole: float) -> float | None:
            return round(part / whole * 100, 1) if whole else None

        return {
            "available": True,
            "period": {
                "start": period[0].isoformat() if period[0] else None,
                "end": period[1].isoformat() if period[1] else None,
                "windowDays": window_days,
            },
            "currency": coverage[2] or "",
            "commitment": {
                "committedEffectiveCost": round(committed, 2),
                "onDemandEffectiveCost": round(on_demand, 2),
                "coveragePercent": pct(committed, committed + on_demand),
                "commitments": [
                    {
                        "id": row[0],
                        "name": row[1] or row[0],
                        "type": row[2] or "",
                        "usedCost": round(float(row[3]), 2),
                        "unusedCost": round(float(row[4]), 2),
                        "utilizationPercent": pct(
                            float(row[3]), float(row[3]) + float(row[4])
                        ),
                    }
                    for row in commitments
                ],
            },
            "pricing": {
                "billedCost": round(billed, 2),
                "effectiveCost": round(effective, 2),
                "listCost": round(list_cost, 2),
                "contractedCost": round(contracted, 2),
                "discountRealized": round(list_cost - effective, 2),
                "discountPercent": pct(list_cost - effective, list_cost),
                "byService": [
                    {
                        "serviceName": row[0],
                        "effectiveCost": round(float(row[1]), 2),
                        "listCost": round(float(row[2]), 2),
                        "billedCost": round(float(row[3]), 2),
                        "discountPercent": pct(
                            float(row[2]) - float(row[1]), float(row[2])
                        ),
                    }
                    for row in by_service
                ],
                "byPricingCategory": [
                    {"name": row[0] or "Unknown", "value": round(float(row[1]), 2)}
                    for row in by_pricing
                ],
            },
        }

    def allocation_config(self) -> dict[str, Any]:
        with self.operational_connect(read_only=True) as db:
            row = db.execute(
                """
                SELECT cost_center_tags, shared_values, unit_tag, unit_label,
                       updated_at
                FROM allocation_config WHERE id = 'default'
                """
            ).fetchone()
        if not row:
            return {
                "costCenterTags": [], "sharedValues": [],
                "unitTag": "", "unitLabel": "", "updatedAt": None,
            }
        return {
            "costCenterTags": json.loads(str(row[0]) or "[]"),
            "sharedValues": json.loads(str(row[1]) or "[]"),
            "unitTag": str(row[2] or ""),
            "unitLabel": str(row[3] or ""),
            "updatedAt": (
                row[4].isoformat()
                if isinstance(row[4], datetime)
                else (str(row[4]) if row[4] else None)
            ),
        }

    def save_allocation_config(
        self,
        cost_center_tags: list[str],
        shared_values: list[str],
        unit_tag: str = "",
        unit_label: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        tags = [tag.strip() for tag in cost_center_tags if tag.strip()]
        shared = [value.strip() for value in shared_values if value.strip()]
        with self.operational_connect() as db:
            db.execute("DELETE FROM allocation_config WHERE id = 'default'")
            db.execute(
                """
                INSERT INTO allocation_config (
                    id, cost_center_tags, shared_values, unit_tag,
                    unit_label, updated_at
                ) VALUES ('default', ?, ?, ?, ?, ?)
                """,
                [
                    json_value(tags), json_value(shared),
                    unit_tag.strip(), unit_label.strip(), now,
                ],
            )
            db.commit()
        return self.allocation_config()

    def ai_intelligence_config(self) -> dict[str, Any]:
        with self.operational_connect(read_only=True) as db:
            row = db.execute(
                """
                SELECT provider, fast_model, deep_model, updated_by, updated_at
                FROM ai_intelligence_config WHERE id = 'default'
                """
            ).fetchone()
        if not row:
            return {
                "provider": "", "fastModel": "", "deepModel": "",
                "updatedBy": "", "updatedAt": None,
            }
        return {
            "provider": str(row[0] or ""),
            "fastModel": str(row[1] or ""),
            "deepModel": str(row[2] or ""),
            "updatedBy": str(row[3] or ""),
            "updatedAt": (
                row[4].isoformat()
                if isinstance(row[4], datetime)
                else (str(row[4]) if row[4] else None)
            ),
        }

    def save_ai_intelligence_config(
        self,
        provider: str,
        fast_model: str,
        deep_model: str,
        updated_by: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        with self.operational_connect() as db:
            db.execute("DELETE FROM ai_intelligence_config WHERE id = 'default'")
            db.execute(
                """
                INSERT INTO ai_intelligence_config (
                    id, provider, fast_model, deep_model, updated_by, updated_at
                ) VALUES ('default', ?, ?, ?, ?, ?)
                """,
                [
                    provider.strip().lower(), fast_model.strip(),
                    deep_model.strip(), updated_by.strip(), now,
                ],
            )
            db.commit()
        return self.ai_intelligence_config()

    def fiscal_outlook_config(self) -> dict[str, Any]:
        with self.operational_connect(read_only=True) as db:
            row = db.execute(
                """
                SELECT fy_start_month, cost_type, growth_percent_monthly,
                       include_planned_savings, savings_ramp_months, notes,
                       updated_by, updated_at
                FROM fiscal_outlook_config WHERE id = 'default'
                """
            ).fetchone()
        if not row:
            return {
                "fyStartMonth": 7,
                "costType": "AmortizedCost",
                "growthPercentMonthly": 0.0,
                "includePlannedSavings": False,
                "savingsRampMonths": 3,
                "notes": "",
                "updatedBy": "",
                "updatedAt": None,
            }
        return {
            "fyStartMonth": int(row[0] or 7),
            "costType": str(row[1] or "AmortizedCost"),
            "growthPercentMonthly": float(row[2] or 0),
            "includePlannedSavings": bool(row[3]),
            "savingsRampMonths": int(row[4] or 0),
            "notes": str(row[5] or ""),
            "updatedBy": str(row[6] or ""),
            "updatedAt": (
                row[7].isoformat()
                if isinstance(row[7], datetime)
                else (str(row[7]) if row[7] else None)
            ),
        }

    def save_fiscal_outlook_config(
        self,
        *,
        fy_start_month: int,
        cost_type: str,
        growth_percent_monthly: float,
        include_planned_savings: bool,
        savings_ramp_months: int,
        notes: str,
        updated_by: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        with self.operational_connect() as db:
            db.execute("DELETE FROM fiscal_outlook_config WHERE id = 'default'")
            db.execute(
                """
                INSERT INTO fiscal_outlook_config (
                    id, fy_start_month, cost_type, growth_percent_monthly,
                    include_planned_savings, savings_ramp_months, notes,
                    updated_by, updated_at
                ) VALUES ('default', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    int(fy_start_month),
                    cost_type.strip(),
                    float(growth_percent_monthly),
                    bool(include_planned_savings),
                    int(savings_ramp_months),
                    notes.strip(),
                    updated_by.strip(),
                    now,
                ],
            )
            db.commit()
        return self.fiscal_outlook_config()

    def planned_rightsizing_monthly_savings(self) -> float:
        """Sum of planner-entered bucket reference savings, for the outlook.

        Scoped to the primary board only: a scratch or exploration board
        (see rightsizing_boards) must never silently inflate the fiscal
        outlook just by existing.
        """
        with self.operational_connect(read_only=True) as db:
            row = db.execute(
                """
                SELECT coalesce(sum(bucket.ref_monthly_savings), 0)
                FROM rightsizing_plan_buckets AS bucket
                JOIN rightsizing_boards AS board
                  ON board.id = bucket.board_id
                WHERE board.is_primary = TRUE
                """
            ).fetchone()
        return float(row[0] or 0) if row else 0.0

    def _monthly_series(
        self,
        cost_type: str,
        currency: str,
        subscription_ids: list[str] | None = None,
    ) -> dict[date, float]:
        conditions = ["cost_type = ?"]
        params: list[Any] = [cost_type]
        if currency:
            conditions.append("currency = ?")
            params.append(currency)
        if subscription_ids is not None:
            if not subscription_ids:
                return {}
            placeholders = ", ".join("?" for _ in subscription_ids)
            conditions.append(f"subscription_id IN ({placeholders})")
            params.extend(value.lower() for value in subscription_ids)
        with self.connect(read_only=True) as db:
            rows = db.execute(
                f"""
                SELECT month, sum(amount) FROM monthly_cost_history
                WHERE {' AND '.join(conditions)}
                GROUP BY month ORDER BY month
                """,
                params,
            ).fetchall()
        return {row[0]: float(row[1] or 0) for row in rows}

    def _fy_current_month_estimate(
        self,
        cost_type: str,
        currency: str,
        months: dict[date, float],
        today: date,
        subscription_ids: list[str] | None = None,
    ) -> float | None:
        """Blend the collected month-to-date total with a daily remainder."""
        current_month = today.replace(day=1)
        month_to_date = months.get(current_month)
        conditions = ["cost_type = ?", "(? = '' OR currency = ?)"]
        params: list[Any] = [cost_type, currency, currency]
        if subscription_ids is not None:
            if not subscription_ids:
                return month_to_date
            placeholders = ", ".join("?" for _ in subscription_ids)
            conditions.append(f"subscription_id IN ({placeholders})")
            params.extend(value.lower() for value in subscription_ids)
        with self.connect(read_only=True) as db:
            daily_rows = db.execute(
                f"""
                SELECT usage_date, sum(amount) FROM daily_cost_history
                WHERE {' AND '.join(conditions)}
                GROUP BY usage_date ORDER BY usage_date
                """,
                params,
            ).fetchall()
        daily_series = {row[0]: float(row[1] or 0) for row in daily_rows}
        if month_to_date is None and daily_series:
            month_to_date = sum(
                amount
                for day, amount in daily_series.items()
                if day >= current_month
            )
        if month_to_date is None:
            return None
        next_month = (
            date(current_month.year + 1, 1, 1)
            if current_month.month == 12
            else date(current_month.year, current_month.month + 1, 1)
        )
        remaining_days = (next_month - today).days
        remainder = 0.0
        if remaining_days > 0 and daily_series:
            daily = forecast_daily_cost(
                daily_series,
                horizon_days=remaining_days + 2,
                as_of=today,
            )
            for point in daily.get("points") or []:
                point_date = date.fromisoformat(point["date"])
                if current_month <= point_date < next_month:
                    remainder += float(point["amount"])
        return float(month_to_date) + remainder

    def budget_groups(self) -> list[dict[str, Any]]:
        with self.operational_connect(read_only=True) as db:
            group_rows = db.execute(
                """
                SELECT id, name, annual_amount, currency, updated_by,
                       updated_at
                FROM budget_groups ORDER BY position, name
                """
            ).fetchall()
            member_rows = db.execute(
                "SELECT group_id, subscription_id FROM budget_group_members"
            ).fetchall()
        members: dict[str, list[str]] = {}
        for group_id, subscription_id in member_rows:
            members.setdefault(str(group_id), []).append(
                str(subscription_id)
            )
        return [
            {
                "id": str(row[0]),
                "name": str(row[1]),
                "annualAmount": float(row[2] or 0),
                "currency": str(row[3] or "USD"),
                "subscriptionIds": sorted(members.get(str(row[0]), [])),
                "updatedBy": str(row[4] or ""),
                "updatedAt": row[5].isoformat() if row[5] else None,
            }
            for row in group_rows
        ]

    def save_budget_groups(
        self, groups: list[dict[str, Any]], updated_by: str = ""
    ) -> list[dict[str, Any]]:
        now = utc_now()
        with self.operational_connect() as db:
            db.execute("DELETE FROM budget_group_members")
            db.execute("DELETE FROM budget_groups")
            for position, group in enumerate(groups):
                group_id = str(group.get("id") or "").strip() or str(uuid4())
                db.execute(
                    """
                    INSERT INTO budget_groups (
                        id, name, annual_amount, currency, position,
                        updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        group_id,
                        str(group.get("name") or "").strip(),
                        float(group.get("annualAmount") or 0),
                        str(group.get("currency") or "USD").strip() or "USD",
                        position,
                        updated_by.strip(),
                        now,
                    ],
                )
                for subscription_id in dict.fromkeys(
                    str(value).strip().lower()
                    for value in group.get("subscriptionIds") or []
                    if str(value).strip()
                ):
                    db.execute(
                        """
                        INSERT INTO budget_group_members (
                            group_id, subscription_id
                        ) VALUES (?, ?)
                        """,
                        [group_id, subscription_id],
                    )
            db.commit()
        return self.budget_groups()

    def fiscal_year_outlook(self, as_of: date | None = None) -> dict[str, Any]:
        """Fiscal-year spend projection from governed monthly actuals.

        Monthly actuals come from monthly_cost_history. The in-progress
        month blends its collected month-to-date total with the daily
        forecaster's remainder so the FY total does not treat a half-elapsed
        month as a full one.
        """
        config = self.fiscal_outlook_config()
        cost_type = config["costType"]
        today = as_of or date.today()
        totals = self.monthly_cost_totals(cost_type)
        months = dict(totals["months"])
        current_estimate = self._fy_current_month_estimate(
            cost_type, totals["currency"], months, today
        )

        planned_available = self.planned_rightsizing_monthly_savings()
        planned_savings = (
            planned_available if config["includePlannedSavings"] else 0.0
        )

        outlook = forecast_fiscal_year(
            months,
            as_of=today,
            fy_start_month=config["fyStartMonth"],
            growth_percent_monthly=config["growthPercentMonthly"],
            planned_savings_monthly=planned_savings,
            savings_ramp_months=config["savingsRampMonths"],
            current_month_estimate=current_estimate,
        )

        budget_monthly = None
        for target in self.budget_targets():
            if target["scopeType"] == "estate":
                budget_monthly = float(target["monthlyAmount"])
                break
        fy_budget = (
            round(budget_monthly * 12, 2) if budget_monthly is not None else None
        )

        configured_items = [
            item
            for item in self.integration().get("subscriptions", [])
            if item.get("subscriptionId")
        ]
        configured_subscriptions = len(configured_items)
        with self.connect(read_only=True) as db:
            covered_row = db.execute(
                """
                SELECT count(DISTINCT subscription_id)
                FROM monthly_cost_history WHERE cost_type = ?
                """,
                [cost_type],
            ).fetchone()
            coverage_rows = db.execute(
                """
                WITH monthly AS (
                    SELECT lower(subscription_id) AS subscription_id,
                           count(DISTINCT month) AS history_months,
                           min(month) AS first_month,
                           max(month) AS last_month,
                           max(observed_at) AS observed_at
                    FROM monthly_cost_history
                    WHERE cost_type = ?
                    GROUP BY lower(subscription_id)
                ), latest_scope AS (
                    SELECT lower(subscription_id) AS subscription_id,
                           status, row_count, status_code, message,
                           completed_at, started_at,
                           row_number() OVER (
                               PARTITION BY lower(subscription_id)
                               ORDER BY started_at DESC
                           ) AS row_number
                    FROM cost_history_scope_runs
                    WHERE cost_type = ?
                )
                SELECT monthly.subscription_id, monthly.history_months,
                       monthly.first_month, monthly.last_month,
                       monthly.observed_at, latest_scope.status,
                       latest_scope.row_count, latest_scope.status_code,
                       latest_scope.message, latest_scope.completed_at
                FROM monthly
                LEFT JOIN latest_scope
                  ON latest_scope.subscription_id = monthly.subscription_id
                 AND latest_scope.row_number = 1
                """,
                [cost_type, cost_type],
            ).fetchall()
        covered_subscriptions = int(covered_row[0] or 0) if covered_row else 0

        coverage_by_subscription = {
            str(row[0]).lower(): {
                "historyMonths": int(row[1] or 0),
                "firstMonth": row[2].isoformat() if row[2] else None,
                "lastMonth": row[3].isoformat() if row[3] else None,
                "observedAt": row[4].isoformat() if row[4] else None,
                "lastIngestionStatus": row[5],
                "lastIngestionRowCount": int(row[6] or 0),
                "lastIngestionStatusCode": row[7],
                "lastIngestionMessage": row[8] or "",
                "lastIngestionCompletedAt": row[9].isoformat() if row[9] else None,
            }
            for row in coverage_rows
        }
        coverage_details = []
        for item in configured_items:
            subscription_id = str(item["subscriptionId"]).lower()
            history = coverage_by_subscription.get(subscription_id)
            coverage_details.append({
                "subscriptionId": item["subscriptionId"],
                "label": item.get("label") or item["subscriptionId"],
                "status": "covered" if history else "no_monthly_history",
                **(history or {
                    "historyMonths": 0,
                    "firstMonth": None,
                    "lastMonth": None,
                    "observedAt": None,
                    "lastIngestionStatus": None,
                    "lastIngestionRowCount": 0,
                    "lastIngestionStatusCode": None,
                    "lastIngestionMessage": "No monthly cost history row exists for this subscription.",
                    "lastIngestionCompletedAt": None,
                }),
            })
        uncovered_subscriptions = [
            item for item in coverage_details if item["status"] != "covered"
        ]

        limitations = []
        if (
            configured_subscriptions
            and covered_subscriptions < configured_subscriptions
        ):
            limitations.append(
                f"Monthly history covers {covered_subscriptions} of "
                f"{configured_subscriptions} configured subscriptions, so "
                "totals understate the estate until the backfill completes."
            )
        if totals["otherCurrencies"]:
            limitations.append(
                "Amounts in "
                + ", ".join(totals["otherCurrencies"])
                + " are excluded; the outlook covers the dominant currency "
                + "only."
            )
        if outlook["historyMonths"] < 12:
            limitations.append(
                f"{outlook['historyMonths']} complete months of history are "
                "available; seasonal comparison strengthens as Cost "
                "Management's thirteen-month window accumulates."
            )
        if planned_savings > 0:
            limitations.append(
                "Projection subtracts the right-sizing plan's "
                "planner-entered reference savings; those are planning "
                "assumptions, not governed calculations."
            )

        return {
            **outlook,
            "costType": cost_type,
            "currency": totals["currency"] or "USD",
            "budgetMonthly": budget_monthly,
            "fyBudget": fy_budget,
            "fyVarianceVsBudget": (
                round(outlook["fyTotal"] - fy_budget, 2)
                if fy_budget is not None
                else None
            ),
            "plannedSavingsMonthly": round(planned_available, 2),
            "planSavingsApplied": bool(
                config["includePlannedSavings"] and planned_savings > 0
            ),
            "subscriptionCoverage": {
                "covered": covered_subscriptions,
                "configured": configured_subscriptions,
                "details": coverage_details,
                "uncovered": uncovered_subscriptions,
            },
            "groups": self._fiscal_group_lanes(
                config, cost_type, totals["currency"], today
            ),
            "config": config,
            "limitations": limitations,
        }

    def _fiscal_group_lanes(
        self,
        config: dict[str, Any],
        cost_type: str,
        currency: str,
        today: date,
    ) -> list[dict[str, Any]]:
        """Per-group fiscal tracking against each group's annual envelope.

        Groups reuse the estate's growth assumption but not its planned
        savings, which are estate-level planning numbers that cannot be
        attributed to one group.
        """
        lanes = []
        for group in self.budget_groups():
            members = group["subscriptionIds"]
            series = self._monthly_series(cost_type, currency, members)
            estimate = self._fy_current_month_estimate(
                cost_type, currency, series, today, members
            )
            projection = forecast_fiscal_year(
                series,
                as_of=today,
                fy_start_month=config["fyStartMonth"],
                growth_percent_monthly=config["growthPercentMonthly"],
                current_month_estimate=estimate,
            )
            covered = len(
                {
                    month_sub
                    for month_sub in self._group_covered_members(
                        cost_type, members
                    )
                }
            )
            annual = float(group["annualAmount"])
            lanes.append(
                {
                    "id": group["id"],
                    "name": group["name"],
                    "currency": group["currency"],
                    "annualBudget": annual,
                    "actualToDate": projection["actualToDate"],
                    "fyTotal": projection["fyTotal"],
                    "fyLower": projection["fyLower"],
                    "fyUpper": projection["fyUpper"],
                    "variance": round(projection["fyTotal"] - annual, 2),
                    "status": projection["status"],
                    "historyMonths": projection["historyMonths"],
                    "memberCount": len(members),
                    "coveredMembers": covered,
                }
            )
        return lanes

    def _group_covered_members(
        self, cost_type: str, subscription_ids: list[str]
    ) -> list[str]:
        if not subscription_ids:
            return []
        placeholders = ", ".join("?" for _ in subscription_ids)
        with self.connect(read_only=True) as db:
            rows = db.execute(
                f"""
                SELECT DISTINCT subscription_id FROM monthly_cost_history
                WHERE cost_type = ?
                  AND subscription_id IN ({placeholders})
                """,
                [cost_type, *[value.lower() for value in subscription_ids]],
            ).fetchall()
        return [str(row[0]) for row in rows]

    # ------------------------------------------------------------------
    # Right-sizing purchase plan (kanban board over live inventory).
    # Pseudo-buckets are stable keys, not rows in the buckets table.
    RIGHTSIZING_SPECIAL_BUCKETS = (
        "__unassigned__",
        "__nodata__",
        "__review__",
        "__savingsplan__",
        "__excluded__",
    )

    @staticmethod
    def _resolve_rightsizing_board_readonly(
        db: Any, board_id: str
    ) -> str | None:
        """Resolve a board id using only reads; None means "would need to
        write" (no board_id given and either no boards exist yet, or none
        is flagged primary).

        DuckDB's development (non-PostgreSQL) operational store opens a
        fresh connection per call with no locking shared with the
        analytical store's connection lock, so a writable connection
        racing a concurrent read-only one against the same file raises
        "different configuration" errors -- reproduced live pairing this
        with the Overview page's concurrent polling. Callers that only
        ever need to read (the plan board, the log) must stay read-only
        in the overwhelmingly common case where a board already exists;
        only the one-time cold-start/repair path below escalates.
        """
        if board_id:
            return board_id
        row = db.execute(
            "SELECT id FROM rightsizing_boards WHERE is_primary = TRUE "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row:
            return str(row[0])
        row = db.execute(
            "SELECT id FROM rightsizing_boards ORDER BY created_at LIMIT 1"
        ).fetchone()
        return str(row[0]) if row else None

    def _resolve_rightsizing_board(self, db: Any, board_id: str) -> str:
        """Resolve a possibly-blank board id to a real one.

        A blank id -- every pre-boards caller, and a switcher that hasn't
        loaded yet -- means "the primary board", lazily created here on
        first use so a fresh install needs no separate seed step. Takes an
        already-open, writable operational connection; the caller commits.
        Prefer _resolve_rightsizing_board_readonly when the caller doesn't
        otherwise need a writable connection (see its docstring for why).
        """
        if board_id:
            return board_id
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
            # A board exists but somehow none is flagged primary; promote
            # the oldest rather than creating a second board.
            board_id = str(row[0])
            db.execute(
                "UPDATE rightsizing_boards SET is_primary = TRUE WHERE id = ?",
                [board_id],
            )
            return board_id
        new_id = str(uuid4())
        now = utc_now()
        db.execute(
            """
            INSERT INTO rightsizing_boards (
                id, name, description, is_primary, created_by,
                created_at, updated_at
            ) VALUES (?, 'Default', '', TRUE, 'system', ?, ?)
            """,
            [new_id, now, now],
        )
        return new_id

    def _ensure_rightsizing_board(self, board_id: str) -> str:
        """Writable fallback for the cold-start/repair case only -- opens
        its own operational connection, so callers only pay for it when
        _resolve_rightsizing_board_readonly returned None."""
        with self.operational_connect() as db:
            resolved = self._resolve_rightsizing_board(db, board_id)
            db.commit()
        return resolved

    @staticmethod
    def _rightsizing_board_row(row: Any) -> dict[str, Any]:
        return {
            "id": str(row[0]),
            "name": str(row[1]),
            "description": str(row[2] or ""),
            "isPrimary": bool(row[3]),
            "createdBy": str(row[4] or ""),
            "createdAt": row[5].isoformat() if row[5] else None,
            "updatedAt": row[6].isoformat() if row[6] else None,
        }

    def rightsizing_boards(self) -> list[dict[str, Any]]:
        """Every board, with lightweight per-board counts for a switcher."""
        with self.operational_connect(read_only=True) as db:
            any_board = db.execute(
                "SELECT 1 FROM rightsizing_boards LIMIT 1"
            ).fetchone()
            rows = (
                db.execute(
                    "SELECT id, name, description, is_primary, created_by, "
                    "created_at, updated_at FROM rightsizing_boards "
                    "ORDER BY is_primary DESC, created_at"
                ).fetchall()
                if any_board
                else []
            )
            bucket_counts = dict(
                db.execute(
                    "SELECT board_id, count(*) FROM rightsizing_plan_buckets "
                    "GROUP BY board_id"
                ).fetchall()
            )
            assigned_counts = dict(
                db.execute(
                    """
                    SELECT board_id, count(*)
                    FROM rightsizing_plan_assignments
                    WHERE bucket_key NOT IN ('__unassigned__', '__nodata__')
                    GROUP BY board_id
                    """
                ).fetchall()
            )
        if not any_board:
            # Cold start only: bootstrap the Default board, then this is a
            # single, cheap re-query (see rightsizing_plan_board's
            # docstring for why the writable path stays isolated).
            self._ensure_rightsizing_board("")
            with self.operational_connect(read_only=True) as db:
                rows = db.execute(
                    "SELECT id, name, description, is_primary, created_by, "
                    "created_at, updated_at FROM rightsizing_boards "
                    "ORDER BY is_primary DESC, created_at"
                ).fetchall()
        boards = [self._rightsizing_board_row(row) for row in rows]
        for board in boards:
            board["bucketCount"] = int(bucket_counts.get(board["id"], 0))
            board["assignedCount"] = int(assigned_counts.get(board["id"], 0))
        return boards

    def create_rightsizing_board(
        self, name: str, description: str = "", actor: str = ""
    ) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("A board name is required.")
        description = description.strip()
        now = utc_now()
        board_id = str(uuid4())
        with self.operational_connect() as db:
            db.execute(
                """
                INSERT INTO rightsizing_boards (
                    id, name, description, is_primary, created_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, FALSE, ?, ?, ?)
                """,
                [board_id, name, description, actor, now, now],
            )
            db.commit()
        return {
            "id": board_id,
            "name": name,
            "description": description,
            "isPrimary": False,
            "createdBy": actor,
            "createdAt": now.isoformat(),
            "updatedAt": now.isoformat(),
            "bucketCount": 0,
            "assignedCount": 0,
        }

    def rename_rightsizing_board(
        self, board_id: str, name: str, description: str = "",
    ) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("A board name is required.")
        description = description.strip()
        now = utc_now()
        with self.operational_connect() as db:
            self._assert_editable_board(db, board_id)
            existing = db.execute(
                "SELECT id FROM rightsizing_boards WHERE id = ?", [board_id]
            ).fetchone()
            if not existing:
                raise ValueError(f"Board {board_id!r} does not exist.")
            db.execute(
                "UPDATE rightsizing_boards SET name = ?, description = ?, "
                "updated_at = ? WHERE id = ?",
                [name, description, now, board_id],
            )
            db.commit()
        return {"id": board_id, "name": name, "description": description}

    def set_primary_rightsizing_board(self, board_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.operational_connect() as db:
            self._assert_editable_board(db, board_id)
            existing = db.execute(
                "SELECT id FROM rightsizing_boards WHERE id = ?", [board_id]
            ).fetchone()
            if not existing:
                raise ValueError(f"Board {board_id!r} does not exist.")
            db.execute(
                "UPDATE rightsizing_boards SET is_primary = FALSE, "
                "updated_at = ? WHERE is_primary = TRUE",
                [now],
            )
            db.execute(
                "UPDATE rightsizing_boards SET is_primary = TRUE, "
                "updated_at = ? WHERE id = ?",
                [now, board_id],
            )
            db.commit()
        return {"id": board_id, "isPrimary": True}

    def delete_rightsizing_board(self, board_id: str) -> dict[str, Any]:
        """Delete a board and everything on it.

        Refuses to remove the primary board: the fiscal outlook and the
        resource evidence dossier always read the primary board, so leaving
        none flagged primary would silently zero them out rather than
        raising. Promote another board first.
        """
        with self.operational_connect() as db:
            self._assert_editable_board(db, board_id)
            row = db.execute(
                "SELECT is_primary FROM rightsizing_boards WHERE id = ?",
                [board_id],
            ).fetchone()
            if not row:
                raise ValueError(f"Board {board_id!r} does not exist.")
            if bool(row[0]):
                raise PermissionError(
                    "Set another board as primary before deleting this "
                    "one -- the fiscal outlook and AI evidence always "
                    "track the primary board."
                )
            buckets_removed = db.execute(
                "SELECT count(*) FROM rightsizing_plan_buckets "
                "WHERE board_id = ?",
                [board_id],
            ).fetchone()[0]
            assignments_removed = db.execute(
                "SELECT count(*) FROM rightsizing_plan_assignments "
                "WHERE board_id = ?",
                [board_id],
            ).fetchone()[0]
            db.execute(
                "DELETE FROM rightsizing_plan_log WHERE board_id = ?",
                [board_id],
            )
            db.execute(
                "DELETE FROM rightsizing_plan_assignments WHERE board_id = ?",
                [board_id],
            )
            db.execute(
                "DELETE FROM rightsizing_plan_buckets WHERE board_id = ?",
                [board_id],
            )
            db.execute(
                "DELETE FROM rightsizing_boards WHERE id = ?", [board_id]
            )
            db.commit()
        return {
            "removed": board_id,
            "bucketsRemoved": int(buckets_removed),
            "assignmentsRemoved": int(assignments_removed),
        }

    @staticmethod
    def _rightsizing_bucket_row(row: Any) -> dict[str, Any]:
        return {
            "bucketKey": str(row[0]),
            "boardId": str(row[1]),
            "region": str(row[2]),
            "sku": str(row[3]),
            "strategy": str(row[4] or ""),
            "source": str(row[5] or ""),
            "refQuantity": int(row[6]) if row[6] is not None else None,
            "refMonthlyPayg": float(row[7]) if row[7] is not None else None,
            "refMonthlyRi1y": float(row[8]) if row[8] is not None else None,
            "refRi1yUpfront": float(row[9]) if row[9] is not None else None,
            "refMonthlySp1y": float(row[10]) if row[10] is not None else None,
            "refMonthlySavings": float(row[11]) if row[11] is not None else None,
            "refReservationCheck": str(row[12] or ""),
            "note": str(row[13] or ""),
            "createdBy": str(row[14] or ""),
            "createdAt": row[15].isoformat() if row[15] else None,
            "updatedAt": row[16].isoformat() if row[16] else None,
        }

    def rightsizing_plan_board(self, board_id: str = "") -> dict[str, Any]:
        """The full planning board: live VM seed joined with plan state.

        The VM list always comes from current inventory (snapshot reads on
        the web), never from a frozen export -- a plan drawn over stale VMs
        quietly plans machines that no longer exist. Plan state lives in the
        operational store so every planner sees the same board. A blank
        board_id resolves to the primary board (see rightsizing_boards).
        """
        with self.connect(read_only=True) as db:
            vm_rows = db.execute(
                """
                WITH cpu AS (
                    SELECT resource_id, p95, coverage_percent, source,
                           window_days
                    FROM (
                        SELECT resource_id, p95, coverage_percent, source,
                            greatest(
                                date_diff('day', window_start, window_end) + 1,
                                1
                            ) AS window_days,
                            row_number() OVER (
                                PARTITION BY resource_id
                                ORDER BY CASE source
                                    WHEN 'azure_monitor' THEN 1 ELSE 2 END,
                                    observed_at DESC
                            ) AS source_rank
                        FROM telemetry_metric_summaries_current
                        WHERE lower(metric) = 'percentage cpu'
                    )
                    WHERE source_rank = 1
                ), focus_manifest_rank AS (
                    SELECT manifest_id,
                           row_number() OVER (
                               PARTITION BY subscription_id,
                                   date_trunc('month', period_start)
                               ORDER BY period_end DESC, imported_at DESC
                           ) AS manifest_rank
                    FROM focus_export_manifests
                    WHERE status = 'imported'
                ), focus_vm_charges AS (
                    SELECT cost.*
                    FROM focus_cost_charges AS cost
                    JOIN focus_manifest_rank AS manifest
                      ON manifest.manifest_id = cost.manifest_id
                     AND manifest.manifest_rank = 1
                    WHERE lower(cost.service_name) = 'virtual machines'
                      AND lower(cost.meter_category) = 'virtual machines'
                      AND lower(cost.service_category) = 'compute'
                ), focus_bounds AS (
                    SELECT
                        greatest(
                            max(CAST(charge_period_start AS DATE))
                                - INTERVAL 29 DAY,
                            min(CAST(charge_period_start AS DATE))
                        ) AS window_start,
                        max(CAST(charge_period_start AS DATE)) AS window_end
                    FROM focus_vm_charges
                ), focus AS (
                    SELECT lower(cost.resource_id) AS resource_id,
                           sum(
                               CASE
                                   WHEN cost.list_cost IS NOT NULL
                                    AND abs(cost.list_cost) > 0.000000001
                                   THEN cost.list_cost
                                   WHEN cost.pricing_quantity IS NOT NULL
                                    AND cost.list_unit_price IS NOT NULL
                                   THEN cost.pricing_quantity
                                        * cost.list_unit_price
                                   ELSE cost.billed_cost
                               END
                           )
                               * 30.0 / nullif(
                                   date_diff(
                                       'day', bounds.window_start,
                                       bounds.window_end
                                   ) + 1,
                                   0
                               ) AS monthly_list_cost,
                           sum(cost.effective_cost) * 30.0 / nullif(
                               date_diff(
                                   'day', bounds.window_start,
                                   bounds.window_end
                               ) + 1,
                               0
                           ) AS monthly_effective_cost,
                           date_diff(
                               'day', bounds.window_start,
                               bounds.window_end
                           ) + 1 AS window_days
                    FROM focus_vm_charges AS cost
                    CROSS JOIN focus_bounds AS bounds
                    WHERE cost.resource_id <> ''
                      AND CAST(cost.charge_period_start AS DATE)
                          BETWEEN bounds.window_start AND bounds.window_end
                    GROUP BY lower(cost.resource_id), bounds.window_start,
                             bounds.window_end
                )
                SELECT
                    lower(resource.resource_id),
                    resource.name,
                    resource.subscription_name,
                    resource.resource_group,
                    resource.region,
                    resource.sku,
                    resource.estimated_monthly_cost,
                    rec.kind,
                    rec.current_sku,
                    rec.target_sku,
                    rec.cpu_p95,
                    rec.metric_coverage_percent,
                    rec.evidence_window_days,
                    rec.estimated_monthly_saving,
                    rec.reason,
                    cpu.p95,
                    cpu.coverage_percent,
                    cpu.source,
                    cpu.window_days,
                    COALESCE(
                        json_extract_string(resource.raw_json, '$.osType'),
                        ''
                    ),
                    COALESCE(
                        json_extract_string(
                            resource.raw_json, '$.licenseType'
                        ),
                        ''
                    ),
                    focus.monthly_list_cost,
                    focus.monthly_effective_cost,
                    focus.window_days
                FROM resources_current AS resource
                LEFT JOIN rightsizing_recommendations_current AS rec
                  ON lower(rec.resource_id) = lower(resource.resource_id)
                LEFT JOIN cpu
                  ON cpu.resource_id = lower(resource.resource_id)
                LEFT JOIN focus
                  ON focus.resource_id = lower(resource.resource_id)
                WHERE lower(resource.resource_type)
                    = 'microsoft.compute/virtualmachines'
                ORDER BY resource.name
                """
            ).fetchall()
        vms = []
        for row in vm_rows:
            cpu_p95 = row[10] if row[10] is not None else row[15]
            profile, license_model = price_profile(row[19], row[20])
            vms.append(
                {
                    "vmKey": str(row[0]),
                    "name": str(row[1]),
                    "subscriptionName": str(row[2] or ""),
                    "resourceGroup": str(row[3] or ""),
                    "region": str(row[4] or ""),
                    "sku": str(row[5] or row[8] or ""),
                    "estimatedMonthlyCost": (
                        float(row[21]) if row[21] is not None
                        else float(row[6]) if row[6] is not None else None
                    ),
                    "observedMonthlyListCost": (
                        float(row[21]) if row[21] is not None else None
                    ),
                    "observedMonthlyEffectiveCost": (
                        float(row[22]) if row[22] is not None else None
                    ),
                    "costWindowDays": (
                        int(row[23]) if row[23] is not None else None
                    ),
                    "operatingSystem": str(row[19] or "").lower(),
                    "licenseModel": license_model,
                    "priceProfile": profile,
                    "action": str(row[7] or "none"),
                    "targetSku": str(row[9] or ""),
                    "cpuP95": float(cpu_p95) if cpu_p95 is not None else None,
                    "coveragePercent": (
                        float(row[11] if row[11] is not None else row[16])
                        if (row[11] is not None or row[16] is not None)
                        else None
                    ),
                    "windowDays": (
                        int(row[12]) if row[12] is not None
                        else int(row[18]) if row[18] is not None
                        else None
                    ),
                    "estimatedMonthlySaving": (
                        float(row[13]) if row[13] is not None else None
                    ),
                    "reason": str(row[14] or ""),
                    "telemetrySource": str(row[17] or ""),
                    "noData": cpu_p95 is None and row[7] is None,
                }
            )
        with self.operational_connect(read_only=True) as db:
            resolved_board_id = self._resolve_rightsizing_board_readonly(
                db, board_id
            )
            board_row, bucket_rows, assignment_rows = None, [], []
            if resolved_board_id:
                board_row = db.execute(
                    "SELECT name, description FROM rightsizing_boards WHERE id = ?",
                    [resolved_board_id],
                ).fetchone()
                bucket_rows = db.execute(
                    """
                    SELECT bucket_key, board_id, region, sku, strategy,
                           source, ref_quantity, ref_monthly_payg,
                           ref_monthly_ri_1y, ref_ri_1y_upfront,
                           ref_monthly_sp_1y, ref_monthly_savings,
                           ref_reservation_check, note, created_by,
                           created_at, updated_at
                    FROM rightsizing_plan_buckets
                    WHERE board_id = ?
                    ORDER BY region, sku
                    """,
                    [resolved_board_id],
                ).fetchall()
                assignment_rows = db.execute(
                    """
                    SELECT vm_key, vm_name, bucket_key, decision, note,
                           ref_monthly_payg, ref_monthly_commitment,
                           ref_monthly_savings, economics_status,
                           updated_by, updated_at
                    FROM rightsizing_plan_assignments
                    WHERE board_id = ?
                    """,
                    [resolved_board_id],
                ).fetchall()
        # Cold start (no board exists anywhere yet) or repair (a board
        # exists but none is primary): the rare path that needs a write,
        # isolated so the common case above never opens a writable
        # connection at all. A newly bootstrapped board is always named
        # "Default" (see _resolve_rightsizing_board), so no re-fetch needed.
        if not resolved_board_id:
            board_id = self._ensure_rightsizing_board(board_id)
            board_row = ("Default", "")
        else:
            board_id = resolved_board_id
        buckets = [self._rightsizing_bucket_row(row) for row in bucket_rows]
        seed_keys = {vm["vmKey"] for vm in vms}
        assignments: dict[str, dict[str, Any]] = {}
        imported_unmatched = []
        for row in assignment_rows:
            entry = {
                "bucketKey": str(row[2]),
                "decision": str(row[3] or ""),
                "note": str(row[4] or ""),
                "refMonthlyPayg": (
                    float(row[5]) if row[5] is not None else None
                ),
                "refMonthlyCommitment": (
                    float(row[6]) if row[6] is not None else None
                ),
                "refMonthlySavings": (
                    float(row[7]) if row[7] is not None else None
                ),
                "economicsStatus": str(row[8] or ""),
                "updatedBy": str(row[9] or ""),
                "updatedAt": row[10].isoformat() if row[10] else None,
            }
            key = str(row[0])
            if key in seed_keys:
                assignments[key] = entry
            else:
                # Preserved from an import but absent from live inventory
                # (decommissioned since, or never an Azure VM). Shown
                # separately rather than silently dropped.
                imported_unmatched.append({**entry, "vmKey": key, "vmName": str(row[1] or "")})
        assigned = sum(
            1
            for value in assignments.values()
            if value["bucketKey"] not in ("__unassigned__", "__nodata__")
        )
        bucket_savings = sum(
            bucket["refMonthlySavings"] or 0 for bucket in buckets
        )
        savings_plan_savings = sum(
            assignment.get("refMonthlySavings") or 0
            for assignment in assignments.values()
            if assignment["bucketKey"] == "__savingsplan__"
        )
        planned_savings = bucket_savings + savings_plan_savings
        modeled_reservation_buckets = sum(
            bucket["refMonthlySavings"] is not None for bucket in buckets
        )
        savings_plan_assignments = [
            assignment for assignment in assignments.values()
            if assignment["bucketKey"] == "__savingsplan__"
        ]
        return {
            "boardId": board_id,
            "boardName": str(board_row[0]) if board_row else "",
            "boardDescription": str(board_row[1] or "") if board_row else "",
            "vms": vms,
            "buckets": buckets,
            "assignments": assignments,
            "importedUnmatched": imported_unmatched,
            "summary": {
                "totalVms": len(vms),
                "assigned": assigned,
                "noData": sum(1 for vm in vms if vm["noData"]),
                "bucketCount": len(buckets),
                "plannedMonthlySavings": round(planned_savings, 2),
                "modeledReservationBuckets": modeled_reservation_buckets,
                "savingsPlanCandidates": len(savings_plan_assignments),
                "modeledSavingsPlanCandidates": sum(
                    assignment.get("refMonthlySavings") is not None
                    for assignment in savings_plan_assignments
                ),
            },
        }

    FLUX_PROPOSAL_ACTOR = "flux"

    def _assert_editable_board(self, db: Any, board_id: str) -> None:
        row = db.execute(
            "SELECT created_by FROM rightsizing_boards WHERE id = ?",
            [board_id],
        ).fetchone()
        if row and str(row[0]) == self.FLUX_PROPOSAL_ACTOR:
            raise PermissionError(
                "The Flux proposal board is read-only. Copy it to an "
                "editable board to make changes."
            )

    def replace_flux_proposal_board(
        self,
        *,
        board_name: str,
        description: str,
        actor: str,
        buckets: list[dict[str, Any]],
        assignments: list[dict[str, Any]],
        summary_note: str,
    ) -> str:
        """Create or wholesale-refresh the system proposal board.

        The proposal is regenerated, never edited: contents are replaced in
        one transaction and the decision log keeps exactly one entry per
        refresh instead of a synthetic move history.
        """
        now = utc_now()
        specials = set(self.RIGHTSIZING_SPECIAL_BUCKETS)
        with self.operational_connect() as db:
            row = db.execute(
                "SELECT id FROM rightsizing_boards "
                "WHERE created_by = ? AND name = ?",
                [actor, board_name],
            ).fetchone()
            if row:
                board_id = str(row[0])
                db.execute(
                    "UPDATE rightsizing_boards SET description = ?, "
                    "updated_at = ? WHERE id = ?",
                    [description, now, board_id],
                )
            else:
                board_id = str(uuid4())
                db.execute(
                    """
                    INSERT INTO rightsizing_boards (
                        id, name, description, is_primary, created_by,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, FALSE, ?, ?, ?)
                    """,
                    [board_id, board_name, description, actor, now, now],
                )
            db.execute(
                "DELETE FROM rightsizing_plan_assignments WHERE board_id = ?",
                [board_id],
            )
            db.execute(
                "DELETE FROM rightsizing_plan_buckets WHERE board_id = ?",
                [board_id],
            )
            db.execute(
                "DELETE FROM rightsizing_plan_log WHERE board_id = ?",
                [board_id],
            )
            for bucket in buckets:
                key = f"{board_id}:{bucket['region']}|{bucket['sku']}"
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_buckets (
                        bucket_key, board_id, region, sku, strategy, source,
                        ref_quantity, ref_monthly_payg, ref_monthly_ri_1y,
                        ref_ri_1y_upfront, ref_monthly_sp_1y,
                        ref_monthly_savings, ref_reservation_check, note,
                        created_by, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, 'flux-proposal', ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        key,
                        board_id,
                        bucket["region"],
                        bucket["sku"],
                        str(bucket.get("strategy") or ""),
                        (
                            int(bucket["refQuantity"])
                            if bucket.get("refQuantity") is not None
                            else None
                        ),
                        bucket.get("refMonthlyPayg"),
                        bucket.get("refMonthlyRi1y"),
                        bucket.get("refRi1yUpfront"),
                        bucket.get("refMonthlySp1y"),
                        bucket.get("refMonthlySavings"),
                        str(bucket.get("refReservationCheck") or ""),
                        str(bucket.get("note") or ""),
                        actor,
                        now,
                        now,
                    ],
                )
            for assignment_index, assignment in enumerate(assignments):
                bucket_key = str(assignment["bucketKey"])
                if bucket_key not in specials:
                    bucket_key = f"{board_id}:{bucket_key}"
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_assignments (
                        board_id, vm_key, vm_name, subscription_name,
                        bucket_key, decision, note, ref_monthly_payg,
                        ref_monthly_commitment, ref_monthly_savings,
                        economics_status, source, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'flux-proposal', ?, ?)
                    """,
                    [
                        board_id,
                        assignment["vmKey"],
                        assignment.get("vmName") or "",
                        assignment.get("subscriptionName") or "",
                        bucket_key,
                        str(assignment.get("decision") or "Pending"),
                        str(assignment.get("note") or ""),
                        assignment.get("refMonthlyPayg"),
                        assignment.get("refMonthlyCommitment"),
                        assignment.get("refMonthlySavings"),
                        str(assignment.get("economicsStatus") or ""),
                        actor,
                        now,
                    ],
                )
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_log (
                        id, board_id, ts, actor, vm_key, vm_name,
                        from_label, to_label, decision, note
                    ) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?)
                    """,
                    [
                        str(uuid4()),
                        board_id,
                        now + timedelta(microseconds=assignment_index),
                        actor,
                        assignment["vmKey"],
                        assignment.get("vmName") or "",
                        bucket_key,
                        str(assignment.get("decision") or "Pending"),
                        str(assignment.get("note") or ""),
                    ],
                )
            db.execute(
                """
                INSERT INTO rightsizing_plan_log (
                    id, board_id, ts, actor, vm_key, vm_name, from_label,
                    to_label, decision, note
                ) VALUES (?, ?, ?, ?, '', '', '', '', '', ?)
                """,
                [
                    str(uuid4()), board_id,
                    now + timedelta(microseconds=len(assignments) + 1),
                    actor, summary_note,
                ],
            )
            db.commit()
        return board_id

    def duplicate_rightsizing_board(
        self, source_board_id: str, name: str, actor: str = ""
    ) -> dict[str, Any]:
        """Copy a board's buckets and placements into a new editable board."""
        name = name.strip()
        if not name:
            raise ValueError("A name for the copied board is required.")
        now = utc_now()
        new_id = str(uuid4())
        specials = set(self.RIGHTSIZING_SPECIAL_BUCKETS)
        with self.operational_connect() as db:
            source = db.execute(
                "SELECT name FROM rightsizing_boards WHERE id = ?",
                [source_board_id],
            ).fetchone()
            if not source:
                raise ValueError("The source board does not exist.")
            db.execute(
                """
                INSERT INTO rightsizing_boards (
                    id, name, description, is_primary, created_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, FALSE, ?, ?, ?)
                """,
                [
                    new_id,
                    name,
                    f"Copied from {source[0]} on {now.date().isoformat()}.",
                    actor,
                    now,
                    now,
                ],
            )
            bucket_rows = db.execute(
                """
                SELECT bucket_key, region, sku, strategy, source,
                       ref_quantity, ref_monthly_payg, ref_monthly_ri_1y,
                       ref_ri_1y_upfront, ref_monthly_sp_1y,
                       ref_monthly_savings, ref_reservation_check, note
                FROM rightsizing_plan_buckets WHERE board_id = ?
                """,
                [source_board_id],
            ).fetchall()
            key_map: dict[str, str] = {}
            for row in bucket_rows:
                suffix = str(row[0]).split(":", 1)[-1]
                new_key = f"{new_id}:{suffix}"
                key_map[str(row[0])] = new_key
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_buckets (
                        bucket_key, board_id, region, sku, strategy, source,
                        ref_quantity, ref_monthly_payg, ref_monthly_ri_1y,
                        ref_ri_1y_upfront, ref_monthly_sp_1y,
                        ref_monthly_savings, ref_reservation_check, note,
                        created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [new_key, new_id, *row[1:13], actor, now, now],
                )
            assignment_rows = db.execute(
                """
                SELECT vm_key, vm_name, subscription_name, bucket_key,
                       decision, note, ref_monthly_payg,
                       ref_monthly_commitment, ref_monthly_savings,
                       economics_status
                FROM rightsizing_plan_assignments WHERE board_id = ?
                """,
                [source_board_id],
            ).fetchall()
            for row in assignment_rows:
                bucket_key = str(row[3])
                if bucket_key not in specials:
                    bucket_key = key_map.get(
                        bucket_key, f"{new_id}:{bucket_key.split(':', 1)[-1]}"
                    )
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_assignments (
                        board_id, vm_key, vm_name, subscription_name,
                        bucket_key, decision, note, ref_monthly_payg,
                        ref_monthly_commitment, ref_monthly_savings,
                        economics_status, source, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'copy', ?, ?)
                    """,
                    [new_id, row[0], row[1], row[2], bucket_key, row[4],
                     row[5], row[6], row[7], row[8], row[9], actor, now],
                )
            db.execute(
                """
                INSERT INTO rightsizing_plan_log (
                    id, board_id, ts, actor, vm_key, vm_name, from_label,
                    to_label, decision, note
                ) VALUES (?, ?, ?, ?, '', '', '', '', '', ?)
                """,
                [
                    str(uuid4()),
                    new_id,
                    now,
                    actor,
                    f"Board copied from {source[0]} "
                    f"({len(bucket_rows)} buckets, "
                    f"{len(assignment_rows)} placements).",
                ],
            )
            db.commit()
        return {"id": new_id, "name": name}

    def save_rightsizing_bucket(
        self, payload: dict[str, Any], updated_by: str = ""
    ) -> dict[str, Any]:
        region = str(payload.get("region") or "").strip()
        sku = str(payload.get("sku") or "").strip()
        now = utc_now()

        def number(name: str) -> float | None:
            value = payload.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                # The standalone tool serialized economics as strings and
                # left unpriced fields as "" -- blank means absent, not zero.
                return None
            return float(value)

        with self.operational_connect() as db:
            board_id = self._resolve_rightsizing_board(
                db, str(payload.get("boardId") or "")
            )
            self._assert_editable_board(db, board_id)
            key = f"{board_id}:{region}|{sku}"
            existing = db.execute(
                "SELECT created_by, created_at FROM rightsizing_plan_buckets "
                "WHERE bucket_key = ?",
                [key],
            ).fetchone()
            db.execute(
                "DELETE FROM rightsizing_plan_buckets WHERE bucket_key = ?",
                [key],
            )
            db.execute(
                """
                INSERT INTO rightsizing_plan_buckets (
                    bucket_key, board_id, region, sku, strategy, source,
                    ref_quantity, ref_monthly_payg, ref_monthly_ri_1y,
                    ref_ri_1y_upfront, ref_monthly_sp_1y,
                    ref_monthly_savings, ref_reservation_check, note,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    key,
                    board_id,
                    region,
                    sku,
                    str(payload.get("strategy") or "").strip(),
                    str(payload.get("source") or "ui"),
                    (
                        int(float(payload["refQuantity"]))
                        if payload.get("refQuantity") not in (None, "")
                        else None
                    ),
                    number("refMonthlyPayg"),
                    number("refMonthlyRi1y"),
                    number("refRi1yUpfront"),
                    number("refMonthlySp1y"),
                    number("refMonthlySavings"),
                    str(payload.get("refReservationCheck") or "").strip(),
                    str(payload.get("note") or "").strip(),
                    existing[0] if existing else updated_by,
                    existing[1] if existing else now,
                    now,
                ],
            )
            db.commit()
        return {"bucketKey": key, "boardId": board_id}

    def delete_rightsizing_bucket(
        self, bucket_key: str, actor: str = ""
    ) -> dict[str, Any]:
        now = utc_now()
        with self.operational_connect() as db:
            bucket_row = db.execute(
                "SELECT board_id FROM rightsizing_plan_buckets "
                "WHERE bucket_key = ?",
                [bucket_key],
            ).fetchone()
            if not bucket_row:
                raise ValueError(f"Bucket {bucket_key!r} does not exist.")
            board_id = str(bucket_row[0])
            self._assert_editable_board(db, board_id)
            moved = db.execute(
                "SELECT count(*) FROM rightsizing_plan_assignments "
                "WHERE bucket_key = ? AND board_id = ?",
                [bucket_key, board_id],
            ).fetchone()[0]
            db.execute(
                "UPDATE rightsizing_plan_assignments "
                "SET bucket_key = '__unassigned__', updated_by = ?, "
                "updated_at = ? WHERE bucket_key = ? AND board_id = ?",
                [actor, now, bucket_key, board_id],
            )
            db.execute(
                "DELETE FROM rightsizing_plan_buckets WHERE bucket_key = ?",
                [bucket_key],
            )
            db.execute(
                """
                INSERT INTO rightsizing_plan_log (
                    id, board_id, ts, actor, vm_key, vm_name, from_label,
                    to_label, decision, note
                ) VALUES (?, ?, ?, ?, '', '', ?, 'Unassigned', '', ?)
                """,
                [
                    str(uuid4()),
                    board_id,
                    now,
                    actor,
                    bucket_key,
                    f"Bucket removed; {int(moved)} VM(s) returned to Unassigned.",
                ],
            )
            db.commit()
        return {"removed": bucket_key, "movedToUnassigned": int(moved)}

    def assign_rightsizing_vms(
        self,
        moves: list[dict[str, Any]],
        board_id: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        with self.operational_connect() as db:
            board_id = self._resolve_rightsizing_board(db, board_id)
            self._assert_editable_board(db, board_id)
            for move in moves:
                vm_key = str(move.get("vmKey") or "").strip().lower()
                bucket_key = str(move.get("bucketKey") or "__unassigned__")
                previous = db.execute(
                    "SELECT bucket_key, decision, note "
                    "FROM rightsizing_plan_assignments "
                    "WHERE board_id = ? AND vm_key = ?",
                    [board_id, vm_key],
                ).fetchone()
                decision = str(
                    move.get("decision")
                    if move.get("decision") is not None
                    else (previous[1] if previous else "Pending")
                )
                note = str(
                    move.get("note")
                    if move.get("note") is not None
                    else (previous[2] if previous else "")
                )
                db.execute(
                    "DELETE FROM rightsizing_plan_assignments "
                    "WHERE board_id = ? AND vm_key = ?",
                    [board_id, vm_key],
                )
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_assignments (
                        board_id, vm_key, vm_name, subscription_name,
                        bucket_key, decision, note, source, updated_by,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ui', ?, ?)
                    """,
                    [
                        board_id,
                        vm_key,
                        str(move.get("vmName") or ""),
                        str(move.get("subscriptionName") or ""),
                        bucket_key,
                        decision,
                        note,
                        actor,
                        now,
                    ],
                )
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_log (
                        id, board_id, ts, actor, vm_key, vm_name,
                        from_label, to_label, decision, note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        str(uuid4()),
                        board_id,
                        now,
                        actor,
                        vm_key,
                        str(move.get("vmName") or ""),
                        str(previous[0]) if previous else "__unassigned__",
                        bucket_key,
                        decision,
                        note,
                    ],
                )
            db.commit()
        return {"moved": len(moves), "boardId": board_id}

    def rightsizing_plan_log(
        self, board_id: str = "", limit: int = 250
    ) -> list[dict[str, Any]]:
        with self.operational_connect(read_only=True) as db:
            resolved_board_id = self._resolve_rightsizing_board_readonly(
                db, board_id
            )
            rows = (
                db.execute(
                    """
                    SELECT ts, actor, vm_key, vm_name, from_label, to_label,
                           decision, note
                    FROM rightsizing_plan_log
                    WHERE board_id = ?
                    ORDER BY ts DESC
                    LIMIT ?
                    """,
                    [resolved_board_id, int(limit)],
                ).fetchall()
                if resolved_board_id
                else []
            )
        return [
            {
                "ts": row[0].isoformat() if row[0] else None,
                "actor": str(row[1] or ""),
                "vmKey": str(row[2] or ""),
                "vmName": str(row[3] or ""),
                "fromLabel": str(row[4] or ""),
                "toLabel": str(row[5] or ""),
                "decision": str(row[6] or ""),
                "note": str(row[7] or ""),
            }
            for row in rows
        ]

    # Bucket economics fields the importer can set, keyed by Flux's own
    # names; used identically for diffing (dry run) and writing (apply).
    _RIGHTSIZING_BUCKET_FIELDS = (
        "strategy", "refQuantity", "refMonthlyPayg", "refMonthlyRi1y",
        "refRi1yUpfront", "refMonthlySp1y", "refMonthlySavings",
        "refReservationCheck", "note",
    )

    @staticmethod
    def _normalize_rightsizing_bucket_field(name: str, value: Any) -> Any:
        if name in ("strategy", "refReservationCheck", "note"):
            return str(value or "").strip()
        if name == "refQuantity":
            return int(float(value)) if value not in (None, "") else None
        # Economics fields: blank string means absent, not zero -- the same
        # rule save_rightsizing_bucket applies when it writes.
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return round(float(value), 2)

    def import_rightsizing_plan(
        self,
        payload: dict[str, Any],
        *,
        board_id: str = "",
        new_board_name: str = "",
        dry_run: bool = False,
        actor: str = "import",
    ) -> dict[str, Any]:
        """Import (or preview importing) the standalone board's files.

        The standalone tool identified VMs by LogicMonitor id (``lm-1402``),
        which means nothing to Flux. Matching goes through VM name plus
        subscription against live inventory; a VM that no longer exists is
        kept under an ``import:`` key rather than dropped, because a decision
        about a machine that was since decommissioned is still part of the
        plan's history.

        The standalone tool stays in use alongside Flux, so re-importing a
        refreshed export is routine, not one-time -- every import can run as
        a dry run first (``dry_run=True``): it resolves VMs and classifies
        every bucket and assignment as added/changed/unchanged against the
        target board and returns that classification without writing
        anything, so a planner can see exactly what an updated file would
        change before committing to it. ``new_board_name`` targets a fresh
        board instead of an existing one (created for real only when
        ``dry_run`` is false); leaving both ``board_id`` and
        ``new_board_name`` blank targets the primary board.
        """
        new_board_name = new_board_name.strip()
        target_is_new = bool(new_board_name) and not board_id
        vms = payload.get("vms") or []
        by_id = {
            str(vm.get("id")): vm for vm in vms if vm.get("id") is not None
        }
        with self.connect(read_only=True) as db:
            inventory = db.execute(
                """
                SELECT lower(resource_id), lower(name),
                       lower(coalesce(subscription_name, '')),
                       lower(coalesce(resource_group, '')),
                       lower(coalesce(region, ''))
                FROM resources_current
                WHERE lower(resource_type) = 'microsoft.compute/virtualmachines'
                """
            ).fetchall()
        by_name: dict[str, list[tuple[str, ...]]] = {}
        by_short: dict[str, list[tuple[str, ...]]] = {}
        inventory_names: list[str] = []
        for row in inventory:
            entry = tuple(str(value or "") for value in row)
            inventory_names.append(entry[1])
            by_name.setdefault(entry[1], []).append(entry)
            short = entry[1].split(".", 1)[0]
            if short != entry[1]:
                by_short.setdefault(short, []).append(entry)

        def narrow(
            candidates: list[tuple[str, ...]], vm: dict[str, Any]
        ) -> list[tuple[str, ...]]:
            # Tighten same-name candidates by subscription, then resource
            # group, then region, stopping as soon as one remains. A filter
            # that eliminates everything is ignored: the tool's labels come
            # from LogicMonitor and may disagree with inventory.
            for field, index in (
                ("subscriptionName", 2),
                ("resourceGroup", 3),
                ("region", 4),
            ):
                if len(candidates) == 1:
                    return candidates
                value = str(vm.get(field) or "").strip().lower()
                if value:
                    narrowed = [c for c in candidates if c[index] == value]
                    if narrowed:
                        candidates = narrowed
            return candidates

        unmatched_names: list[str] = []

        def resolve(lm_id: str) -> tuple[str, str, bool]:
            vm = by_id.get(lm_id) or {}
            display = str(vm.get("vmName") or "").strip()
            # The tool's vmName is the LogicMonitor device name, which may be
            # a FQDN or the guest hostname rather than the Azure resource
            # name; computerName is the guest hostname when known. Try each
            # as given and as a domain-stripped short name, both directions.
            for raw in (vm.get("vmName"), vm.get("computerName")):
                name = str(raw or "").strip().lower()
                if not name:
                    continue
                short = name.split(".", 1)[0]
                candidates = (
                    by_name.get(name)
                    or by_name.get(short)
                    or by_short.get(short)
                    or []
                )
                candidates = narrow(candidates, vm)
                if len(candidates) == 1:
                    return candidates[0][0], display or name, True
            return f"import:{lm_id}", display or lm_id, False

        buckets = payload.get("buckets") or {}
        if isinstance(buckets, dict):
            bucket_pairs = [
                (str(key), value) for key, value in buckets.items()
            ]
        else:
            bucket_pairs = [
                (str(value.get("key") or ""), value) for value in buckets
            ]

        # Human labels for the diff preview -- the file's own bucket keys
        # (bare "region|sku") and the four fixed pseudo-buckets, keyed by
        # the exact string that appears in an incoming assignment.
        bucket_label_by_raw_key: dict[str, str] = {
            "__unassigned__": "Unassigned",
            "__nodata__": "No monitoring data",
            "__review__": "Keep on demand",
            "__savingsplan__": "Savings plan",
            "__excluded__": "Excluded",
        }
        for key, bucket in bucket_pairs:
            bucket_key_in = str(bucket.get("key") or key)
            region = str(bucket.get("region") or "").strip()
            sku = str(bucket.get("sku") or "").strip()
            if (
                bucket_key_in in self.RIGHTSIZING_SPECIAL_BUCKETS
                or not region
                or not sku
            ):
                continue
            bucket_label_by_raw_key[f"{region}|{sku}"] = f"{sku} — {region}"

        def bare_bucket_ref(key: str) -> str:
            # An assignment's bucket key arrives bare ("region|sku") from
            # the standalone tool, but re-importing Flux's own "Export"
            # backup carries the previous board-prefixed key instead
            # ("<board-id>:region|sku", possibly a different board's id).
            # Stripping down to the part after the last ":" normalizes
            # either shape to the bare form before it's re-prefixed or
            # used to look up a label -- region/sku names never contain
            # ":" themselves, so this is unambiguous.
            if key in self.RIGHTSIZING_SPECIAL_BUCKETS:
                return key
            return key.rsplit(":", 1)[-1]

        # Resolve every incoming assignment/vmMeta key to a live (or
        # preserved) vm_key up front, once -- the diff and the write both
        # need the identical resolution.
        assignments_in = payload.get("assignments") or {}
        vm_meta = payload.get("vmMeta") or payload.get("vm_meta") or {}
        resolved_moves: list[dict[str, Any]] = []
        matched = unmatched = 0
        for lm_id in set(assignments_in) | set(vm_meta):
            bucket_key = str(assignments_in.get(lm_id) or "__unassigned__")
            meta = vm_meta.get(lm_id) or {}
            decision = str(meta.get("decision") or "Pending")
            note = str(meta.get("note") or "")
            if bucket_key == "__unassigned__" and not (
                note or decision not in ("", "Pending")
            ):
                # Default state with nothing attached carries no signal.
                continue
            vm_key, vm_name, resolved = resolve(str(lm_id))
            matched += 1 if resolved else 0
            unmatched += 0 if resolved else 1
            if not resolved:
                unmatched_names.append(vm_name)
            vm = by_id.get(str(lm_id)) or {}
            resolved_moves.append(
                {
                    "lmId": str(lm_id),
                    "bucketLabel": bucket_label_by_raw_key.get(
                        bare_bucket_ref(bucket_key), bucket_key
                    ),
                    "vmKey": vm_key,
                    "vmName": vm_name,
                    "subscriptionName": str(vm.get("subscriptionName") or ""),
                    "bucketKey": bucket_key,
                    "decision": decision,
                    "note": note,
                    "resolved": resolved,
                }
            )

        with self.operational_connect() as db:
            resolved_board_id = (
                None if target_is_new
                else self._resolve_rightsizing_board(db, board_id)
            )
            if resolved_board_id:
                self._assert_editable_board(db, resolved_board_id)
            existing_buckets: dict[str, tuple[Any, ...]] = {}
            existing_assignments: dict[str, tuple[Any, ...]] = {}
            existing_import_log_count = 0
            if resolved_board_id:
                existing_buckets = {
                    str(row[0]): row
                    for row in db.execute(
                        """
                        SELECT bucket_key, region, sku, strategy,
                               ref_quantity, ref_monthly_payg,
                               ref_monthly_ri_1y, ref_ri_1y_upfront,
                               ref_monthly_sp_1y, ref_monthly_savings,
                               ref_reservation_check, note
                        FROM rightsizing_plan_buckets WHERE board_id = ?
                        """,
                        [resolved_board_id],
                    ).fetchall()
                }
                existing_assignments = {
                    str(row[0]): row
                    for row in db.execute(
                        "SELECT vm_key, bucket_key, decision, note "
                        "FROM rightsizing_plan_assignments "
                        "WHERE board_id = ?",
                        [resolved_board_id],
                    ).fetchall()
                }
                existing_import_log_count = db.execute(
                    "SELECT count(*) FROM rightsizing_plan_log "
                    "WHERE board_id = ? AND actor LIKE 'import%'",
                    [resolved_board_id],
                ).fetchone()[0]
            db.commit()

        if resolved_board_id:
            # The file's bucket keys are usually bare "region|sku" strings,
            # but re-importing Flux's own export carries an already
            # board-prefixed key (see save_rightsizing_bucket); bare_bucket_ref
            # normalizes either shape before re-prefixing onto the target
            # board so a re-import never double-prefixes. Special
            # pseudo-buckets carry no region/sku and are never prefixed.
            for move in resolved_moves:
                if move["bucketKey"] not in self.RIGHTSIZING_SPECIAL_BUCKETS:
                    move["bucketKey"] = (
                        f"{resolved_board_id}:"
                        f"{bare_bucket_ref(move['bucketKey'])}"
                    )

        # ---- Classify buckets: added / changed / unchanged / skipped ----
        normalize = self._normalize_rightsizing_bucket_field
        buckets_added: list[dict[str, Any]] = []
        buckets_changed: list[dict[str, Any]] = []
        buckets_unchanged = 0
        buckets_skipped = 0
        bucket_writes: list[dict[str, Any]] = []
        for key, bucket in bucket_pairs:
            bucket_key_in = str(bucket.get("key") or key)
            region = str(bucket.get("region") or "").strip()
            sku = str(bucket.get("sku") or "").strip()
            if (
                bucket_key_in in self.RIGHTSIZING_SPECIAL_BUCKETS
                or not region
                or not sku
            ):
                # The tool serializes its fixed pseudo-columns (savings plan,
                # excluded, ...) alongside real buckets. Those columns always
                # exist on the board; importing one as a regular bucket
                # produced a duplicate "Savings Plan (all eligible VMs)"
                # column.
                buckets_skipped += 1
                continue
            incoming_raw = {
                "strategy": bucket.get("strategy"),
                "refQuantity": bucket.get("refQuantity"),
                "refMonthlyPayg": bucket.get("refMonthlyPaygBaseline"),
                "refMonthlyRi1y": bucket.get("refMonthlyRi1YearCost"),
                "refRi1yUpfront": bucket.get("refRi1YearUpfrontTotal"),
                "refMonthlySp1y": bucket.get("refMonthlySp1YearCost"),
                "refMonthlySavings": bucket.get("refMonthlySavingsVsPayg"),
                "refReservationCheck": bucket.get(
                    "refExistingReservationCheck"
                ),
                "note": bucket.get("note"),
            }
            bucket_writes.append(
                {"region": region, "sku": sku, "raw": incoming_raw}
            )
            full_key = (
                f"{resolved_board_id}:{region}|{sku}"
                if resolved_board_id else ""
            )
            label = f"{sku} — {region}"
            existing_row = existing_buckets.get(full_key)
            if existing_row is None:
                buckets_added.append(
                    {"label": label, "region": region, "sku": sku}
                )
                continue
            stored_raw = {
                "strategy": existing_row[3],
                "refQuantity": existing_row[4],
                "refMonthlyPayg": existing_row[5],
                "refMonthlyRi1y": existing_row[6],
                "refRi1yUpfront": existing_row[7],
                "refMonthlySp1y": existing_row[8],
                "refMonthlySavings": existing_row[9],
                "refReservationCheck": existing_row[10],
                "note": existing_row[11],
            }
            changed_fields = [
                {
                    "field": name,
                    "before": normalize(name, stored_raw[name]),
                    "after": normalize(name, incoming_raw[name]),
                }
                for name in self._RIGHTSIZING_BUCKET_FIELDS
                if normalize(name, stored_raw[name])
                != normalize(name, incoming_raw[name])
            ]
            if changed_fields:
                buckets_changed.append(
                    {
                        "label": label, "region": region, "sku": sku,
                        "fields": changed_fields,
                    }
                )
            else:
                buckets_unchanged += 1

        # Labels for buckets already on the board, keyed by their real
        # (board-prefixed) bucket_key -- for the "before" side of a changed
        # assignment, which stores the real key, not the file's bare one.
        bucket_label_by_full_key: dict[str, str] = {
            "__unassigned__": "Unassigned",
            "__nodata__": "No monitoring data",
            "__review__": "Keep on demand",
            "__savingsplan__": "Savings plan",
            "__excluded__": "Excluded",
        }
        for full_key, row in existing_buckets.items():
            bucket_label_by_full_key[full_key] = f"{row[2]} — {row[1]}"

        # ---- Classify assignments: added / changed / unchanged ----
        assignments_added: list[dict[str, Any]] = []
        assignments_changed: list[dict[str, Any]] = []
        assignments_unchanged = 0
        for move in resolved_moves:
            existing_row = existing_assignments.get(move["vmKey"])
            if existing_row is None:
                assignments_added.append(
                    {
                        "vmKey": move["vmKey"],
                        "vmName": move["vmName"],
                        "bucketKey": move["bucketKey"],
                        "bucketLabel": move["bucketLabel"],
                        "decision": move["decision"],
                        "note": move["note"],
                        "resolved": move["resolved"],
                    }
                )
                continue
            before = {
                "bucketKey": str(existing_row[1]),
                "decision": str(existing_row[2] or ""),
                "note": str(existing_row[3] or ""),
            }
            after = {
                "bucketKey": move["bucketKey"],
                "decision": move["decision"],
                "note": move["note"],
            }
            if before != after:
                assignments_changed.append(
                    {
                        "vmKey": move["vmKey"], "vmName": move["vmName"],
                        "before": {
                            **before,
                            "bucketLabel": bucket_label_by_full_key.get(
                                before["bucketKey"], before["bucketKey"]
                            ),
                        },
                        "after": {
                            **after, "bucketLabel": move["bucketLabel"],
                        },
                    }
                )
            else:
                assignments_unchanged += 1

        incoming_log = payload.get("log") or []

        if dry_run:
            return {
                "dryRun": True,
                "boardId": resolved_board_id,
                "newBoardName": new_board_name or None,
                "buckets": {
                    "added": buckets_added,
                    "changed": buckets_changed,
                    "unchanged": buckets_unchanged,
                    "skipped": buckets_skipped,
                },
                "assignments": {
                    "added": assignments_added,
                    "changed": assignments_changed,
                    "unchanged": assignments_unchanged,
                },
                "logEntriesReplaced": existing_import_log_count,
                "logEntriesIncoming": len(incoming_log),
                "matched": matched,
                "unmatched": unmatched,
                "unmatchedSamples": unmatched_names[:5],
                "inventorySample": sorted(inventory_names)[:5],
                "inventoryVmCount": len(inventory_names),
            }

        # ---- Real apply, reusing the resolution and diff work above ----
        if target_is_new:
            board = self.create_rightsizing_board(new_board_name, actor=actor)
            resolved_board_id = board["id"]

        buckets_imported = 0
        for write in bucket_writes:
            buckets_imported += 1
            raw = write["raw"]
            self.save_rightsizing_bucket(
                {
                    "boardId": resolved_board_id,
                    "region": write["region"],
                    "sku": write["sku"],
                    "strategy": raw["strategy"],
                    "source": "import",
                    "refQuantity": raw["refQuantity"],
                    "refMonthlyPayg": raw["refMonthlyPayg"],
                    "refMonthlyRi1y": raw["refMonthlyRi1y"],
                    "refRi1yUpfront": raw["refRi1yUpfront"],
                    "refMonthlySp1y": raw["refMonthlySp1y"],
                    "refMonthlySavings": raw["refMonthlySavings"],
                    "refReservationCheck": raw["refReservationCheck"],
                    "note": raw["note"],
                },
                updated_by=actor,
            )

        now = utc_now()
        imported = 0
        with self.operational_connect() as db:
            for move in resolved_moves:
                lm_id = move["lmId"]
                vm_key = move["vmKey"]
                if move["resolved"]:
                    # A previous import may have preserved this VM under its
                    # import: key before matching worked; promote it.
                    db.execute(
                        "DELETE FROM rightsizing_plan_assignments "
                        "WHERE board_id = ? AND vm_key = ?",
                        [resolved_board_id, f"import:{lm_id}"],
                    )
                db.execute(
                    "DELETE FROM rightsizing_plan_assignments "
                    "WHERE board_id = ? AND vm_key = ?",
                    [resolved_board_id, vm_key],
                )
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_assignments (
                        board_id, vm_key, vm_name, subscription_name,
                        bucket_key, decision, note, source, updated_by,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'import', ?, ?)
                    """,
                    [
                        resolved_board_id,
                        vm_key,
                        move["vmName"],
                        move["subscriptionName"],
                        move["bucketKey"],
                        move["decision"],
                        move["note"],
                        actor,
                        now,
                    ],
                )
                imported += 1
            log_imported = 0
            # Re-imports replace previously imported history wholesale, or
            # every run would append the same entries again under fresh ids.
            db.execute(
                "DELETE FROM rightsizing_plan_log "
                "WHERE board_id = ? AND actor LIKE 'import%'",
                [resolved_board_id],
            )
            for entry in incoming_log:
                ts_ms = entry.get("ts")
                moment = (
                    datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                    if isinstance(ts_ms, (int, float))
                    else now
                )
                vm_key, vm_name, _ = resolve(str(entry.get("vmId") or ""))
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_log (
                        id, board_id, ts, actor, vm_key, vm_name,
                        from_label, to_label, decision, note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        str(uuid4()),
                        resolved_board_id,
                        moment,
                        actor,
                        vm_key,
                        str(entry.get("vmName") or vm_name),
                        str(entry.get("from") or ""),
                        str(entry.get("to") or ""),
                        str(entry.get("decision") or ""),
                        str(entry.get("note") or ""),
                    ],
                )
                log_imported += 1
            db.commit()
        return {
            "dryRun": False,
            "boardId": resolved_board_id,
            "bucketsImported": buckets_imported,
            "bucketsSkipped": buckets_skipped,
            "assignmentsImported": imported,
            "matched": matched,
            "unmatched": unmatched,
            "logImported": log_imported,
            # When matching fails outright, the two name samples make the
            # cause visible in the UI instead of requiring server access.
            "unmatchedSamples": unmatched_names[:5],
            "inventorySample": sorted(inventory_names)[:5],
            "inventoryVmCount": len(inventory_names),
        }

    def budget_targets(self) -> list[dict[str, Any]]:
        with self.operational_connect(read_only=True) as db:
            rows = db.execute(
                """
                SELECT scope_type, scope_id, monthly_amount, currency,
                       updated_by, updated_at
                FROM budget_targets ORDER BY scope_type, scope_id
                """
            ).fetchall()
        return [
            {
                "scopeType": str(row[0]),
                "scopeId": str(row[1] or ""),
                "monthlyAmount": float(row[2]),
                "currency": str(row[3] or "USD"),
                "updatedBy": str(row[4] or ""),
                "updatedAt": row[5].isoformat() if row[5] else None,
            }
            for row in rows
        ]

    def save_budget_targets(
        self, targets: list[dict[str, Any]], updated_by: str = ""
    ) -> list[dict[str, Any]]:
        now = utc_now()
        with self.operational_connect() as db:
            db.execute("DELETE FROM budget_targets")
            for target in targets:
                db.execute(
                    """
                    INSERT INTO budget_targets (
                        scope_type, scope_id, monthly_amount, currency,
                        updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        target["scopeType"],
                        target.get("scopeId", ""),
                        float(target["monthlyAmount"]),
                        target.get("currency", "USD"),
                        updated_by,
                        now,
                    ],
                )
            db.commit()
        return self.budget_targets()

    def budget_report(self, *, as_of: date | None = None) -> dict[str, Any]:
        """Budget vs finalized actuals vs linear run-rate projection.

        The projection is a deliberately simple, labeled method: finalized
        month-to-date actual extrapolated by average daily spend across the
        remaining calendar days. Status thresholds: projected over budget is
        over; projected above 90% is at risk.
        """
        targets = self.budget_targets()
        if not targets:
            return {"configured": False, "targets": []}
        today = as_of or utc_now().date()
        month_start = today.replace(day=1)
        next_month = (month_start + timedelta(days=32)).replace(day=1)
        days_in_month = (next_month - month_start).days
        # Azure finalizes billing data 24-48 hours late. Anchoring the whole
        # month on that horizon (as before: month_start = horizon.replace(day=1))
        # rolled month_start back into the prior month on the 1st/2nd of a
        # month, silently reporting last month's near-complete totals as this
        # month's MTD against this month's targets (elapsed_days ~= days in
        # the prior month instead of ~0-1) -- the same first-of-month period
        # inversion fixed for cost_report in c617ca3. Anchor on the current
        # calendar month instead, and clamp the finalized horizon so it never
        # precedes month_start: before any day of this month is finalized,
        # there is honestly zero elapsed MTD data yet, not a full month of it.
        finalized_end = today - timedelta(days=2)
        end = finalized_end if finalized_end >= month_start else None
        elapsed_days = (end - month_start).days + 1 if end else 0
        rows = []
        if end:
            with self.connect(read_only=True) as db:
                rows = db.execute(
                    """
                    SELECT subscription_id, COALESCE(SUM(amount), 0)
                    FROM daily_cost_history
                    WHERE cost_type = 'ActualCost'
                      AND usage_date BETWEEN ? AND ?
                    GROUP BY subscription_id
                    """,
                    [month_start, end],
                ).fetchall()
        by_subscription = {str(row[0]).lower(): float(row[1]) for row in rows}
        estate_mtd = sum(by_subscription.values())
        results = []
        for target in targets:
            if target["scopeType"] == "estate":
                mtd = estate_mtd
            else:
                mtd = by_subscription.get(target["scopeId"].lower(), 0.0)
            projected = mtd / elapsed_days * days_in_month if elapsed_days else 0.0
            budget = target["monthlyAmount"]
            burn = round(mtd / budget * 100, 1) if budget else None
            projected_pct = round(projected / budget * 100, 1) if budget else None
            status = "on_track"
            if elapsed_days and projected_pct is not None and projected_pct > 100:
                status = "over"
            elif elapsed_days and projected_pct is not None and projected_pct > 90:
                status = "at_risk"
            results.append(
                {
                    **target,
                    "mtdActual": round(mtd, 2),
                    "projectedMonthly": round(projected, 2),
                    "burnPercent": burn,
                    "projectedPercent": projected_pct,
                    "status": status,
                    "method": "linear run-rate projection over finalized days",
                }
            )
        return {
            "configured": True,
            "period": {
                "monthStart": month_start.isoformat(),
                "finalizedThrough": end.isoformat() if end else None,
                "daysInMonth": days_in_month,
                "elapsedFinalizedDays": elapsed_days,
            },
            "targets": results,
        }

    def unit_economics_report(self) -> dict[str, Any]:
        """Actual MTD spend by the configured business dimension tag."""
        config = self.allocation_config()
        unit_tag = (config.get("unitTag") or "").lower()
        if not unit_tag:
            return {"configured": False, "config": config}
        with self.connect(read_only=True) as db:
            rows = db.execute(
                """
                WITH resource_costs AS (
                    SELECT lower(resource_id) AS resource_id,
                           SUM(CASE WHEN cost_type = 'ActualCost'
                               THEN amount END) AS actual_cost
                    FROM costs_current
                    GROUP BY lower(resource_id)
                )
                SELECT resource.resource_id, resource.name,
                       resource.subscription_id, resource.subscription_name,
                       resource.resource_group, resource.resource_type,
                       resource.region, resource.tags_json,
                       COALESCE(cost.actual_cost, 0)
                FROM resources_current AS resource
                LEFT JOIN resource_costs AS cost
                  ON cost.resource_id = lower(resource.resource_id)
                """
            ).fetchall()
        units: dict[str, dict[str, Any]] = {}
        unattributed = 0.0
        total = 0.0
        rules = self.virtual_tag_rules(include_inactive=False)
        resource_ids = [str(row[0]).lower() for row in rows]
        overrides = self.virtual_tag_overrides_for(resource_ids)
        from .virtual_tags import effective_tags
        for row in rows:
            resource_id, name, subscription_id, subscription_name = row[:4]
            resource_group, resource_type, region, tags_json, cost = row[4:]
            cost = float(cost or 0)
            total += cost
            try:
                native_tags = json.loads(tags_json or "{}") or {}
            except (TypeError, ValueError):
                native_tags = {}
            resolved = effective_tags(
                {
                    "resourceId": resource_id, "name": name,
                    "subscriptionId": subscription_id,
                    "subscriptionName": subscription_name,
                    "resourceGroup": resource_group,
                    "resourceType": resource_type, "region": region,
                    "tags": native_tags,
                },
                rules, overrides.get(str(resource_id).lower(), []), utc_now().date(),
            )
            tags = {
                str(key).lower(): str(item.get("value") or "")
                for key, item in resolved.items()
            }
            value = tags.get(unit_tag)
            if not value:
                unattributed += cost
                continue
            bucket = units.setdefault(
                value, {"name": value, "monthlyCost": 0.0, "resourceCount": 0}
            )
            bucket["monthlyCost"] += cost
            bucket["resourceCount"] += 1
        results = sorted(
            (
                {
                    "name": item["name"],
                    "resourceCount": item["resourceCount"],
                    "monthlyCost": round(item["monthlyCost"], 2),
                    "percentOfTotal": (
                        round(item["monthlyCost"] / total * 100, 1) if total else None
                    ),
                }
                for item in units.values()
            ),
            key=lambda item: item["monthlyCost"],
            reverse=True,
        )
        return {
            "configured": True,
            "config": config,
            "summary": {
                "dimensionLabel": config.get("unitLabel") or config.get("unitTag"),
                "unitCount": len(results),
                "totalMonthlyCost": round(total, 2),
                "unattributedCost": round(unattributed, 2),
                "attributedPercent": (
                    round((total - unattributed) / total * 100, 1) if total else None
                ),
            },
            "units": results,
        }

    def executive_summary(self) -> dict[str, Any]:
        """One governed page for leadership: spend, trajectory, outcomes."""
        comparison = self._overview_period_comparison()
        budgets = self.budget_report()
        savings = self.savings_report()
        allocation = self.allocation_report()
        anomalies: dict[str, Any] = {}
        with self.connect(read_only=True) as db:
            anomaly_row = db.execute(
                """
                SELECT count(*), COALESCE(SUM(current_amount - baseline_median), 0)
                FROM cost_anomalies_current
                WHERE severity <> 'none'
                """
            ).fetchall()
            if anomaly_row:
                anomalies = {
                    "count": int(anomaly_row[0][0] or 0),
                    "dailyIncrease": round(float(anomaly_row[0][1] or 0), 2),
                }
            top_services = db.execute(
                """
                SELECT service_name, COALESCE(SUM(amount), 0)
                FROM daily_cost_history
                WHERE cost_type = 'ActualCost'
                  AND usage_date >= date_trunc('month', current_date - INTERVAL 2 DAY)
                GROUP BY service_name
                ORDER BY 2 DESC
                LIMIT 5
                """
            ).fetchall()
        return {
            "generatedAt": utc_now().isoformat(),
            "spend": comparison,
            "topServices": [
                {"name": row[0], "mtdActual": round(float(row[1]), 2)}
                for row in top_services
            ],
            "budgets": budgets if budgets.get("configured") else None,
            "anomalies": anomalies,
            "savings": savings["summary"],
            "allocation": (
                {
                    "allocatedPercent": allocation["summary"]["allocatedPercent"],
                    "unallocatedCost": allocation["summary"]["unallocatedCost"],
                }
                if allocation.get("configured")
                else None
            ),
            "serviceComposition": self.service_composition_report(),
        }

    def service_composition_report(self) -> dict[str, Any]:
        """Explain provider billing labels versus resource/economic categories."""
        period_start = date.today().replace(day=1)
        with self.connect(read_only=True) as db:
            currencies = db.execute(
                """
                SELECT currency, COALESCE(SUM(amount), 0) AS total
                FROM daily_cost_history
                WHERE cost_type = 'ActualCost' AND usage_date >= ?
                GROUP BY currency ORDER BY total DESC
                """,
                [period_start],
            ).fetchall()
            currency = currencies[0][0] if currencies else "USD"
            billing = db.execute(
                """
                SELECT COALESCE(NULLIF(cost.service_name, ''), 'Unallocated'),
                       COALESCE(SUM(cost.amount), 0)
                FROM daily_cost_history AS cost
                WHERE cost.cost_type = 'ActualCost'
                  AND cost.usage_date >= ? AND cost.currency = ?
                GROUP BY 1 ORDER BY 2 DESC LIMIT 20
                """,
                [period_start, currency],
            ).fetchall()
            economic = db.execute(
                """
                SELECT CASE
                    WHEN lower(resource.resource_type) = 'microsoft.compute/virtualmachines'
                        THEN 'VM compute'
                    WHEN lower(resource.resource_type) = 'microsoft.compute/disks'
                        THEN 'Managed disks'
                    WHEN lower(resource.resource_type) LIKE 'microsoft.storage/%'
                        THEN 'Blob / File storage'
                    WHEN lower(cost.service_name) = 'azure site recovery'
                        THEN 'Backup / ASR'
                    WHEN lower(cost.service_name) LIKE '%network%'
                      OR lower(resource.resource_type) LIKE 'microsoft.network/%'
                        THEN 'Network'
                    WHEN lower(cost.service_name) = 'azure marketplace'
                        THEN 'Marketplace'
                    WHEN lower(cost.service_name) = 'virtual machines'
                        THEN 'VM compute — billing classified'
                    WHEN lower(cost.service_name) LIKE '%storage%'
                        THEN 'Storage — resource unresolved'
                    ELSE 'Other / unresolved'
                END AS category,
                COALESCE(SUM(cost.amount), 0)
                FROM daily_cost_history AS cost
                LEFT JOIN resources_current AS resource
                  ON lower(resource.resource_id) = lower(cost.resource_id)
                WHERE cost.cost_type = 'ActualCost'
                  AND cost.usage_date >= ? AND cost.currency = ?
                GROUP BY 1 ORDER BY 2 DESC
                """,
                [period_start, currency],
            ).fetchall()
            sources = db.execute(
                """
                SELECT source, COALESCE(SUM(amount), 0)
                FROM daily_cost_history
                WHERE cost_type = 'ActualCost' AND usage_date >= ? AND currency = ?
                GROUP BY source ORDER BY 2 DESC
                """,
                [period_start, currency],
            ).fetchall()
        source_names = {str(row[0]) for row in sources if row[0]}
        mixed = len(source_names) > 1
        note = (
            "Billing-service labels are mixed across sources. FOCUS may book managed disks under Virtual Machines, while Cost Management may book them under Storage. Resource/economic categories use the current resource identity where available; Blob versus File is not separable from this daily history alone."
            if mixed
            else
            "Resource/economic categories use current resource identity where available; Blob versus File is not separable from this daily history alone."
        )
        return {
            "periodStart": period_start.isoformat(),
            "currency": currency,
            "billingServices": [
                {"name": row[0], "amount": round(float(row[1] or 0), 2)}
                for row in billing
            ],
            "economicCategories": [
                {"name": row[0], "amount": round(float(row[1] or 0), 2)}
                for row in economic
            ],
            "sources": [
                {"name": row[0], "amount": round(float(row[1] or 0), 2)}
                for row in sources
            ],
            "mixedSourceClassification": mixed,
            "note": note,
        }

    def allocation_report(self) -> dict[str, Any]:
        """Showback by cost center with proportional shared-cost proration.

        The cost center of a resource is the value of the first configured
        tag key it carries (case-insensitive). Spend in centers named by the
        configured shared values is prorated across the remaining centers in
        proportion to their direct spend; untagged spend stays explicitly
        Unallocated so the coverage gap is never hidden.
        """
        config = self.allocation_config()
        tag_keys = [tag.lower() for tag in config["costCenterTags"]]
        shared_names = {value.lower() for value in config["sharedValues"]}
        if not tag_keys:
            return {"configured": False, "config": config}
        with self.connect(read_only=True) as db:
            rows = db.execute(
                """
                WITH resource_costs AS (
                    SELECT lower(resource_id) AS resource_id,
                           SUM(CASE WHEN cost_type = 'ActualCost'
                               THEN amount END) AS actual_cost
                    FROM costs_current
                    GROUP BY lower(resource_id)
                )
                SELECT resource.tags_json, COALESCE(cost.actual_cost, 0)
                FROM resources_current AS resource
                LEFT JOIN resource_costs AS cost
                  ON cost.resource_id = lower(resource.resource_id)
                """
            ).fetchall()
        centers: dict[str, dict[str, Any]] = {}
        shared_pool = 0.0
        unallocated = 0.0
        unallocated_count = 0
        total = 0.0
        for tags_json, cost in rows:
            cost = float(cost or 0)
            total += cost
            try:
                tags = {
                    str(key).lower(): str(value)
                    for key, value in (json.loads(tags_json or "{}") or {}).items()
                }
            except (TypeError, ValueError):
                tags = {}
            center = next(
                (tags[key] for key in tag_keys if tags.get(key)), None
            )
            if center is None:
                unallocated += cost
                unallocated_count += 1
                continue
            if center.lower() in shared_names:
                shared_pool += cost
                continue
            bucket = centers.setdefault(
                center, {"name": center, "directCost": 0.0, "resourceCount": 0}
            )
            bucket["directCost"] += cost
            bucket["resourceCount"] += 1
        direct_total = sum(item["directCost"] for item in centers.values())
        results = []
        for bucket in centers.values():
            share = (
                shared_pool * bucket["directCost"] / direct_total
                if direct_total
                else 0.0
            )
            results.append(
                {
                    "name": bucket["name"],
                    "resourceCount": bucket["resourceCount"],
                    "directCost": round(bucket["directCost"], 2),
                    "sharedAllocation": round(share, 2),
                    "totalCost": round(bucket["directCost"] + share, 2),
                    "percentOfTotal": (
                        round((bucket["directCost"] + share) / total * 100, 1)
                        if total
                        else None
                    ),
                }
            )
        results.sort(key=lambda item: item["totalCost"], reverse=True)
        return {
            "configured": True,
            "config": config,
            "summary": {
                "totalMonthlyCost": round(total, 2),
                "allocatedPercent": (
                    round((direct_total + shared_pool) / total * 100, 1)
                    if total
                    else None
                ),
                "sharedPool": round(shared_pool, 2),
                "unallocatedCost": round(unallocated, 2),
                "unallocatedResourceCount": unallocated_count,
                "centerCount": len(results),
            },
            "centers": results,
        }

    def tag_hygiene_report(
        self,
        required_tags: tuple[str, ...] = (),
        excluded_types: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Tag coverage, required-tag compliance, and the untagged spend they hide.

        The allocation prerequisite: spend can only be attributed to cost
        centers when the resources carrying it are tagged. Required tags come
        from FLUX_INTELLIGENCE_REQUIRED_TAGS; types in ``excluded_types``
        (platform resources that legitimately carry no tags) are ignored for
        compliance percentages.
        """
        required = [tag.strip() for tag in required_tags if tag.strip()]
        excluded = {item.strip().lower() for item in excluded_types if item.strip()}
        with self.connect(read_only=True) as db:
            rows = db.execute(
                """
                WITH resource_costs AS (
                    SELECT lower(resource_id) AS resource_id,
                           SUM(CASE WHEN cost_type = 'ActualCost'
                               THEN amount END) AS actual_cost
                    FROM costs_current
                    GROUP BY lower(resource_id)
                )
                SELECT
                    resource.subscription_id,
                    any_value(NULLIF(resource.subscription_name, '')),
                    resource.resource_type,
                    resource.resource_id,
                    resource.name,
                    resource.tags_json,
                    COALESCE(cost.actual_cost, 0)
                FROM resources_current AS resource
                LEFT JOIN resource_costs AS cost
                  ON cost.resource_id = lower(resource.resource_id)
                GROUP BY resource.subscription_id, resource.resource_type,
                         resource.resource_id, resource.name,
                         resource.tags_json, cost.actual_cost
                """
            ).fetchall()

        by_subscription: dict[str, dict[str, Any]] = {}
        missing_by_tag: dict[str, int] = {tag: 0 for tag in required}
        top_untagged: list[dict[str, Any]] = []
        totals = {
            "resources": 0, "tagged": 0, "compliant": 0,
            "cost": 0.0, "taggedCost": 0.0, "excluded": 0,
        }
        for (
            subscription_id, subscription_name, resource_type,
            resource_id, name, tags_json, cost,
        ) in rows:
            cost = float(cost or 0)
            if (resource_type or "").lower() in excluded:
                totals["excluded"] += 1
                continue
            try:
                tags = json.loads(tags_json or "{}")
            except (TypeError, ValueError):
                tags = {}
            tag_keys = {str(key).lower() for key in tags} if tags else set()
            has_tags = bool(tag_keys)
            compliant = all(tag.lower() in tag_keys for tag in required) if required else has_tags
            for tag in required:
                if tag.lower() not in tag_keys:
                    missing_by_tag[tag] += 1
            bucket = by_subscription.setdefault(
                subscription_id,
                {
                    "subscriptionId": subscription_id,
                    "subscriptionName": subscription_name or subscription_id,
                    "resources": 0, "tagged": 0, "compliant": 0,
                    "cost": 0.0, "untaggedCost": 0.0,
                },
            )
            bucket["resources"] += 1
            totals["resources"] += 1
            totals["cost"] += cost
            bucket["cost"] += cost
            if has_tags:
                bucket["tagged"] += 1
                totals["tagged"] += 1
                totals["taggedCost"] += cost
            else:
                bucket["untaggedCost"] += cost
                if cost > 0:
                    top_untagged.append(
                        {
                            "resourceId": resource_id,
                            "name": name,
                            "resourceType": resource_type,
                            "subscriptionId": subscription_id,
                            "monthlyCost": round(cost, 2),
                        }
                    )
            if compliant:
                bucket["compliant"] += 1
                totals["compliant"] += 1

        def pct(part: float, whole: float) -> float | None:
            return round(part / whole * 100, 1) if whole else None

        top_untagged.sort(key=lambda item: item["monthlyCost"], reverse=True)
        subscriptions = sorted(
            by_subscription.values(), key=lambda item: item["untaggedCost"], reverse=True
        )
        for bucket in subscriptions:
            bucket["taggedPercent"] = pct(bucket["tagged"], bucket["resources"])
            bucket["compliantPercent"] = pct(bucket["compliant"], bucket["resources"])
            bucket["cost"] = round(bucket["cost"], 2)
            bucket["untaggedCost"] = round(bucket["untaggedCost"], 2)
        return {
            "summary": {
                "resourceCount": totals["resources"],
                "excludedCount": totals["excluded"],
                "taggedPercent": pct(totals["tagged"], totals["resources"]),
                "compliantPercent": pct(totals["compliant"], totals["resources"]),
                "totalMonthlyCost": round(totals["cost"], 2),
                "taggedCostPercent": pct(totals["taggedCost"], totals["cost"]),
                "untaggedMonthlyCost": round(totals["cost"] - totals["taggedCost"], 2),
                "requiredTags": required,
            },
            "missingByRequiredTag": [
                {"tag": tag, "missingCount": count}
                for tag, count in missing_by_tag.items()
            ],
            "bySubscription": subscriptions,
            "topUntagged": top_untagged[:25],
        }

    def workload_report(self) -> dict[str, Any]:
        result = self.opportunities(
            include_governance=True,
            sort="valuation",
            direction="desc",
            limit=50_000,
            offset=0,
        )
        items = result["items"]
        retirement_kinds = {
            "unattached_disk",
            "aged_snapshot",
            "snapshot_source_deleted",
            "public_ip_unattached",
            "public_ip_orphan_nic",
            "empty_standard_load_balancer",
            "empty_application_gateway",
            "vnet_gateway_no_connections",
            "empty_paid_app_service_plan",
            "unused_network_interface",
            "idle_nat_gateway",
            "empty_availability_set",
            "orphaned_network_security_group",
            "basic_public_ip_retired",
            "basic_load_balancer_retired",
        }

        def grouped(key: str) -> list[dict[str, Any]]:
            values: dict[str, dict[str, Any]] = {}
            for item in items:
                label = str(item.get(key) or "Unclassified")
                bucket = values.setdefault(
                    label,
                    {"name": label, "count": 0, "riskAdjustedValue": 0.0},
                )
                bucket["count"] += 1
                bucket["riskAdjustedValue"] += float(
                    item.get("monthlyRiskAdjustedSavings") or 0
                )
            return sorted(
                values.values(),
                key=lambda value: (
                    value["riskAdjustedValue"], value["count"]
                ),
                reverse=True,
            )

        retirement = [
            item for item in items if item.get("kind") in retirement_kinds
        ]
        with self.connect(read_only=True) as db:
            tag_rows = db.execute(
                "SELECT resource_id, tags_json FROM resources_current"
            ).fetchall()
            savings_rows = db.execute(
                """
                SELECT CAST(computed_at AS DATE), currency,
                       sum(COALESCE(monthly_risk_adjusted, 0))
                FROM opportunity_valuation_snapshots_v2
                WHERE computed_at >= current_date - INTERVAL 90 DAY
                  AND valuation_status = 'valued'
                GROUP BY 1, 2
                ORDER BY 1, 2
                """
            ).fetchall()
        tags_by_resource = {
            row[0]: json.loads(row[1]) if row[1] else {}
            for row in tag_rows
        }
        retirement_details = []
        for item in retirement:
            tags = {
                str(key).lower(): value
                for key, value in (
                    tags_by_resource.get(item.get("resourceId"), {}) or {}
                ).items()
            }
            ownership_keys = {
                "owner", "serviceowner", "applicationowner", "costcenter",
                "businessunit",
            }
            retirement_details.append(
                {
                    **item,
                    "ownershipReady": bool(ownership_keys & set(tags)),
                    "ownershipTags": sorted(ownership_keys & set(tags)),
                    "costExposure": item.get("actualMonthlyCost")
                    or item.get("monthlyGrossSavings"),
                    "isServiceRetirement": item.get("kind") in {
                        "basic_public_ip_retired",
                        "basic_load_balancer_retired",
                    },
                }
            )

        def age_band(item: dict[str, Any]) -> str:
            age = item.get("ageDays")
            if age is None:
                return "Unknown"
            if age < 7:
                return "<7 days"
            if age < 30:
                return "7–29 days"
            if age < 90:
                return "30–89 days"
            return "90+ days"

        coverage: dict[str, int] = {}
        for item in items:
            status = str(
                (item.get("confidenceFactors") or {}).get(
                    "telemetryStatus"
                )
                or "not_required_or_unavailable"
            )
            coverage[status] = coverage.get(status, 0) + 1
        telemetry_ready = sum(
            1
            for item in items
            if (
                (item.get("confidenceFactors") or {}).get("telemetryStatus")
                == "covered"
            )
        )
        return {
            "summary": {
                **result["summary"],
                "telemetryReady": telemetry_ready,
                "retirementCandidates": len(retirement),
                "retirementRiskAdjustedValue": round(
                    sum(
                        float(item.get("monthlyRiskAdjustedSavings") or 0)
                        for item in retirement
                    ),
                    2,
                ),
                "serviceRetirementCandidates": sum(
                    1 for item in retirement_details
                    if item["isServiceRetirement"]
                ),
                "ownershipReady": sum(
                    1 for item in retirement_details
                    if item["ownershipReady"]
                ),
            },
            "bySource": grouped("source"),
            "byCategory": grouped("category"),
            "byConfidence": grouped("confidence"),
            "byAge": [
                {
                    "name": label,
                    "count": sum(
                        1 for item in items if age_band(item) == label
                    ),
                }
                for label in (
                    "<7 days", "7–29 days", "30–89 days",
                    "90+ days", "Unknown",
                )
            ],
            "coverageGaps": [
                {"status": key, "count": value}
                for key, value in sorted(coverage.items())
            ],
            "savingsTrend": [
                {
                    "date": row[0].isoformat(),
                    "currency": row[1],
                    "riskAdjustedValue": round(float(row[2] or 0), 2),
                }
                for row in savings_rows
            ],
            "topOpportunities": items[:25],
            "retirementCandidates": retirement_details[:5000],
            "lineage": {
                "sources": [
                    "Azure Advisor",
                    "Flux Signals",
                    "Azure Cost Management",
                    "Azure Monitor and LogicMonitor when covered",
                ],
                "method": (
                    "Governed current opportunities with confidence, "
                    "corroboration, and risk-adjusted valuation."
                ),
            },
        }

    def focus_cost_report(
        self,
        *,
        currency: str = "",
        start_date: date | None = None,
        end_date: date | None = None,
        subscription_id: str = "",
        service_name: str = "",
        resource_id: str = "",
        charge_category: str = "",
        pricing_category: str = "",
        commitment_discount_type: str = "",
    ) -> dict[str, Any]:
        """Return governed FOCUS charge-level cost evidence.

        This report intentionally exposes declared dimensions and measures
        instead of accepting SQL. It complements, rather than replaces, the
        daily cost history used for trends and forecasting.
        """
        integration = self.integration()
        configured = [
            {
                "id": str(item.get("subscriptionId") or "").lower(),
                "name": str(
                    item.get("label") or item.get("subscriptionId") or ""
                ),
            }
            for item in integration.get("subscriptions", [])
            if item.get("subscriptionId")
        ]
        conditions = ["1 = 1"]
        params: list[Any] = []
        if subscription_id:
            conditions.append("lower(cost.subscription_id) = ?")
            params.append(subscription_id.lower())
        if service_name:
            conditions.append("cost.service_name = ?")
            params.append(service_name)
        if resource_id:
            conditions.append("lower(cost.resource_id) = ?")
            params.append(resource_id.lower())
        if charge_category:
            conditions.append("cost.charge_category = ?")
            params.append(charge_category)
        if pricing_category:
            conditions.append("cost.pricing_category = ?")
            params.append(pricing_category)
        if commitment_discount_type:
            conditions.append("cost.commitment_discount_type = ?")
            params.append(commitment_discount_type)
        scope_where = " AND ".join(conditions)

        with self.connect(read_only=True) as db:
            available = db.execute(
                f"""
                SELECT
                    min(CAST(cost.charge_period_start AS DATE)),
                    max(CAST(cost.charge_period_start AS DATE)),
                    count(DISTINCT NULLIF(cost.billing_currency, '')),
                    arg_max(
                        NULLIF(cost.billing_currency, ''),
                        abs(cost.billed_cost)
                    )
                FROM focus_cost_current AS cost
                WHERE {scope_where}
                """,
                params,
            ).fetchone()
            selected_currency = currency or available[3] or ""
            maximum_date = available[1]
            minimum_date = available[0]
            report_end = min(end_date, maximum_date) if (
                end_date and maximum_date
            ) else (end_date or maximum_date)
            report_start = start_date or (
                report_end - timedelta(days=29) if report_end else None
            )
            if minimum_date and report_start:
                report_start = max(report_start, minimum_date)

            period_conditions = list(conditions)
            period_params = list(params)
            if selected_currency:
                period_conditions.append("cost.billing_currency = ?")
                period_params.append(selected_currency)
            if report_start:
                period_conditions.append(
                    "CAST(cost.charge_period_start AS DATE) >= ?"
                )
                period_params.append(report_start)
            if report_end:
                period_conditions.append(
                    "CAST(cost.charge_period_start AS DATE) <= ?"
                )
                period_params.append(report_end)
            period_where = " AND ".join(period_conditions)

            summary = db.execute(
                f"""
                SELECT
                    COALESCE(sum(cost.billed_cost), 0),
                    COALESCE(sum(cost.effective_cost), 0),
                    COALESCE(sum(cost.contracted_cost), 0),
                    COALESCE(sum(cost.list_cost), 0),
                    count(*),
                    count(DISTINCT NULLIF(lower(cost.resource_id), '')),
                    COALESCE(sum(cost.billed_cost) FILTER (
                        WHERE lower(cost.charge_category) = 'purchase'
                    ), 0),
                    COALESCE(sum(cost.billed_cost) FILTER (
                        WHERE lower(cost.charge_category) = 'usage'
                    ), 0),
                    COALESCE(sum(cost.effective_cost) FILTER (
                        WHERE cost.commitment_discount_id <> ''
                           OR cost.commitment_discount_type <> ''
                    ), 0),
                    count(cost.contracted_cost) FILTER (
                        WHERE cost.contracted_cost <> 0
                    ),
                    count(cost.list_cost) FILTER (
                        WHERE cost.list_cost <> 0
                    )
                FROM focus_cost_current AS cost
                WHERE {period_where}
                """,
                period_params,
            ).fetchone()

            def grouped(
                expression: str,
                fallback: str,
                limit: int = 12,
            ) -> list[dict[str, Any]]:
                rows = db.execute(
                    f"""
                    SELECT
                        COALESCE(NULLIF({expression}, ''), ?),
                        sum(cost.billed_cost),
                        sum(cost.effective_cost),
                        count(*)
                    FROM focus_cost_current AS cost
                    WHERE {period_where}
                    GROUP BY 1
                    ORDER BY abs(sum(cost.billed_cost)) DESC
                    LIMIT {limit}
                    """,
                    [fallback, *period_params],
                ).fetchall()
                return [
                    {
                        "name": str(row[0]),
                        "billedCost": round(float(row[1] or 0), 2),
                        "effectiveCost": round(float(row[2] or 0), 2),
                        "chargeCount": int(row[3] or 0),
                    }
                    for row in rows
                ]

            resource_rows = db.execute(
                f"""
                SELECT
                    cost.resource_id,
                    COALESCE(NULLIF(cost.resource_name, ''), cost.resource_id),
                    cost.resource_type,
                    cost.resource_group,
                    cost.subscription_id,
                    COALESCE(
                        NULLIF(cost.subscription_name, ''),
                        cost.subscription_id
                    ),
                    sum(cost.billed_cost),
                    sum(cost.effective_cost),
                    count(*)
                FROM focus_cost_current AS cost
                WHERE {period_where}
                  AND cost.resource_id <> ''
                GROUP BY 1, 2, 3, 4, 5, 6
                ORDER BY abs(sum(cost.billed_cost)) DESC
                LIMIT 25
                """,
                period_params,
            ).fetchall()
            manifest_rows = db.execute(
                """
                SELECT
                    lower(subscription_id),
                    COALESCE(NULLIF(subscription_name, ''), subscription_id),
                    min(period_start),
                    max(period_end),
                    sum(row_count),
                    max(imported_at),
                    string_agg(DISTINCT data_version, ', ')
                FROM focus_manifests_current
                GROUP BY 1, 2
                ORDER BY 2
                """
            ).fetchall()
            by_subscription = grouped(
                "cost.subscription_name", "Unallocated subscription"
            )
            by_service = grouped(
                "cost.service_name", "Unallocated service"
            )
            by_charge_category = grouped(
                "cost.charge_category", "Unallocated category"
            )
            by_pricing_category = grouped(
                "cost.pricing_category", "Unallocated pricing category"
            )
            by_commitment_type = grouped(
                "cost.commitment_discount_type", "No commitment"
            )
            by_sku = grouped("cost.sku_id", "Unallocated SKU")
            by_meter = grouped("cost.meter_name", "Unallocated meter")

        covered_ids = {str(row[0]).lower() for row in manifest_rows}
        configured_ids = {item["id"] for item in configured}
        missing = [
            item for item in configured if item["id"] not in covered_ids
        ]
        unconfigured = [
            {
                "id": str(row[0]),
                "name": str(row[1]),
            }
            for row in manifest_rows
            if str(row[0]).lower() not in configured_ids
        ]
        billed = float(summary[0] or 0)
        effective = float(summary[1] or 0)
        limitations: list[str] = []
        if missing:
            limitations.append(
                "FOCUS export coverage is partial: "
                f"{len(covered_ids & configured_ids)}/{len(configured_ids)} "
                "configured subscriptions have imported manifests; missing "
                + ", ".join(item["name"] for item in missing)
                + "."
            )
        if int(available[2] or 0) > 1 and not currency:
            limitations.append(
                "Multiple billing currencies are present. The response is "
                f"limited to {selected_currency}; currencies are not combined."
            )
        if not int(summary[9] or 0) or not int(summary[10] or 0):
            limitations.append(
                "ContractedCost or ListCost is absent or zero for some CSP "
                "charges; Flux does not infer price savings from missing fields."
            )
        if not maximum_date:
            limitations.append(
                "No FOCUS charge rows match the requested scope and filters."
            )
        return {
            "period": {
                "start": report_start.isoformat() if report_start else None,
                "end": report_end.isoformat() if report_end else None,
                "availableStart": (
                    minimum_date.isoformat() if minimum_date else None
                ),
                "availableEnd": (
                    maximum_date.isoformat() if maximum_date else None
                ),
            },
            "currency": selected_currency or "Unknown",
            "currencyCount": int(available[2] or 0),
            "summary": {
                "billedCost": round(billed, 2),
                "effectiveCost": round(effective, 2),
                "contractedCost": round(float(summary[2] or 0), 2),
                "listCost": round(float(summary[3] or 0), 2),
                "billedVsEffectiveDifference": round(
                    billed - effective, 2
                ),
                "chargeCount": int(summary[4] or 0),
                "resourceCount": int(summary[5] or 0),
                "purchaseBilledCost": round(float(summary[6] or 0), 2),
                "usageBilledCost": round(float(summary[7] or 0), 2),
                "commitmentEffectiveCost": round(
                    float(summary[8] or 0), 2
                ),
            },
            "bySubscription": by_subscription,
            "byService": by_service,
            "byChargeCategory": by_charge_category,
            "byPricingCategory": by_pricing_category,
            "byCommitmentType": by_commitment_type,
            "bySku": by_sku,
            "byMeter": by_meter,
            "resources": [
                {
                    "resourceId": row[0],
                    "resourceName": row[1],
                    "resourceType": row[2],
                    "resourceGroup": row[3],
                    "subscriptionId": row[4],
                    "subscriptionName": row[5],
                    "billedCost": round(float(row[6] or 0), 2),
                    "effectiveCost": round(float(row[7] or 0), 2),
                    "chargeCount": int(row[8] or 0),
                }
                for row in resource_rows
            ],
            "coverage": {
                "complete": bool(configured_ids) and not missing,
                "configuredScopes": len(configured_ids),
                "availableScopes": len(covered_ids & configured_ids),
                "missingScopes": missing,
                "additionalScopes": unconfigured,
                "manifests": [
                    {
                        "subscriptionId": row[0],
                        "subscriptionName": row[1],
                        "periodStart": (
                            row[2].isoformat() if row[2] else None
                        ),
                        "periodEnd": row[3].isoformat() if row[3] else None,
                        "rows": int(row[4] or 0),
                        "importedAt": (
                            row[5].isoformat() if row[5] else None
                        ),
                        "dataVersion": row[6] or "",
                    }
                    for row in manifest_rows
                ],
            },
            "lineage": {
                "source": "Azure Cost Management FOCUS exports",
                "view": "focus_cost_current",
                "grain": "FOCUS charge line",
                "standard": "FOCUS 1.0",
            },
            "limitations": limitations,
        }

    def cost_report(
        self,
        *,
        cost_type: str = "AmortizedCost",
        currency: str = "",
        start_date: date | None = None,
        end_date: date | None = None,
        subscription_id: str = "",
        service_name: str = "",
        resource_id: str = "",
        forecast_latency_days: int = 2,
        forecast_horizon_days: int = 30,
    ) -> dict[str, Any]:
        integration = self.integration()
        configured_subscriptions = [
            {
                "id": str(item.get("subscriptionId") or "").lower(),
                "name": item.get("label") or item.get("subscriptionId") or "",
            }
            for item in integration.get("subscriptions", [])
            if item.get("subscriptionId")
        ]
        scope_conditions = ["cost.cost_type = ?"]
        scope_params: list[Any] = [cost_type]
        if subscription_id:
            scope_conditions.append("cost.subscription_id = ?")
            scope_params.append(subscription_id.lower())
        if service_name:
            scope_conditions.append("cost.service_name = ?")
            scope_params.append(service_name)
        if resource_id:
            scope_conditions.append("cost.resource_id = ?")
            scope_params.append(resource_id.lower())
        scope_where = " AND ".join(scope_conditions)
        with self.connect(read_only=True) as db:
            available = db.execute(
                f"""
                SELECT min(cost.usage_date), max(cost.usage_date),
                       count(DISTINCT NULLIF(cost.currency, ''))
                FROM daily_cost_history AS cost
                WHERE {scope_where}
                """,
                scope_params,
            ).fetchone()
            # Select the majority currency (highest total sum) instead of
            # the currency of the single largest row. arg_max could pick a
            # minority currency and exclude most of the estate's data.
            currency_row = db.execute(
                f"""
                SELECT NULLIF(cost.currency, ''), sum(cost.amount)
                FROM daily_cost_history AS cost
                WHERE {scope_where}
                GROUP BY NULLIF(cost.currency, '')
                ORDER BY sum(cost.amount) DESC
                LIMIT 1
                """,
                scope_params,
            ).fetchone()
            selected_currency = currency or (
                currency_row[0] if currency_row else ""
            ) or ""
            maximum_date = available[1]
            minimum_date = available[0]
            ingested_end = maximum_date
            # Azure Cost Management finalizes billing data 24-48 hours late.
            # Exclude the most recent unfinalized days when the caller has not
            # set an explicit end_date, so the report does not show an
            # artificial current-day drop-off.
            if not end_date and maximum_date:
                maximum_date = maximum_date - timedelta(
                    days=forecast_latency_days
                )
            report_end = min(end_date, maximum_date) if (
                end_date and maximum_date
            ) else (end_date or maximum_date)
            report_start = start_date or (
                report_end - timedelta(days=29) if report_end else None
            )
            if minimum_date and report_start:
                report_start = max(report_start, minimum_date)
            # An explicit start_date in the first days of a month lands beyond
            # the finalized horizon (maximum_date minus the latency window),
            # inverting the period: start 08-01 with end clamped to 07-30
            # returned 0 totals for both periods and a negative period length
            # that placed the "previous" window in the future (seen live
            # 2026-08-01, "0 USD vs 0 USD" month-over-month). Flux reporting
            # is uniformly as-of the finalized horizon (the standard 24-48h
            # billing delay), so the default never serves unfinalized days:
            # the period is reported honestly empty with the as-of date, and
            # a caller that genuinely wants unfinalized rows opts in by
            # passing an explicit end_date (which already bypasses the lag
            # clamp above).
            period_note = ""
            if report_start and report_end and report_start > report_end:
                finalized_text = (
                    report_end.isoformat() if report_end else "none"
                )
                unfinalized_text = (
                    f" Unfinalized rows exist through "
                    f"{ingested_end.isoformat()}; pass an explicit endDate "
                    "to include them."
                    if ingested_end and ingested_end >= report_start
                    else ""
                )
                report_end = None
                period_empty = True
                period_note = (
                    "The requested period has no finalized billing days "
                    f"yet; figures are as of {finalized_text} (Azure "
                    "finalizes billing data 24-48 hours late)."
                    f"{unfinalized_text}"
                )
            else:
                period_empty = False

            period_conditions = list(scope_conditions)
            period_params = list(scope_params)
            if selected_currency:
                period_conditions.append("cost.currency = ?")
                period_params.append(selected_currency)
            if report_start:
                period_conditions.append("cost.usage_date >= ?")
                period_params.append(report_start)
            if report_end:
                period_conditions.append("cost.usage_date <= ?")
                period_params.append(report_end)
            if period_empty:
                # No finalized days in the requested window: without an upper
                # bound the >= start condition would still match unfinalized
                # rows, quietly serving what the as-of contract excludes.
                period_conditions.append("1 = 0")
            period_where = " AND ".join(period_conditions)

            daily_rows = db.execute(
                f"""
                SELECT cost.usage_date, sum(cost.amount)
                FROM daily_cost_history AS cost
                WHERE {period_where}
                GROUP BY cost.usage_date
                ORDER BY cost.usage_date
                """,
                period_params,
            ).fetchall()
            current_total = sum(float(row[1] or 0) for row in daily_rows)
            period_days = (
                (report_end - report_start).days + 1
                if report_start and report_end and report_end >= report_start
                else 0
            )
            previous_total = 0.0
            previous_start = None
            previous_end = None
            if period_days and report_start:
                previous_end = report_start - timedelta(days=1)
                previous_start = previous_end - timedelta(
                    days=period_days - 1
                )
                previous_conditions = list(scope_conditions)
                previous_params = list(scope_params)
                if selected_currency:
                    previous_conditions.append("cost.currency = ?")
                    previous_params.append(selected_currency)
                previous_conditions.extend(
                    ["cost.usage_date >= ?", "cost.usage_date <= ?"]
                )
                previous_params.extend([previous_start, previous_end])
                previous_total = float(
                    db.execute(
                        f"""
                        SELECT COALESCE(sum(cost.amount), 0)
                        FROM daily_cost_history AS cost
                        WHERE {' AND '.join(previous_conditions)}
                        """,
                        previous_params,
                    ).fetchone()[0]
                    or 0
                )

            subscription_rows = db.execute(
                f"""
                WITH names AS (
                    SELECT subscription_id,
                           any_value(NULLIF(subscription_name, '')) AS name
                    FROM resources_current GROUP BY subscription_id
                )
                SELECT cost.subscription_id,
                       COALESCE(names.name, cost.subscription_id),
                       sum(cost.amount)
                FROM daily_cost_history AS cost
                LEFT JOIN names USING (subscription_id)
                WHERE {period_where}
                GROUP BY cost.subscription_id, names.name
                ORDER BY sum(cost.amount) DESC
                LIMIT 12
                """,
                period_params,
            ).fetchall()
            service_rows = db.execute(
                f"""
                SELECT COALESCE(NULLIF(cost.service_name, ''), 'Unallocated'),
                       sum(cost.amount)
                FROM daily_cost_history AS cost
                WHERE {period_where}
                GROUP BY cost.service_name
                ORDER BY sum(cost.amount) DESC
                LIMIT 12
                """,
                period_params,
            ).fetchall()
            resource_group_rows = db.execute(
                f"""
                SELECT COALESCE(NULLIF(resource.resource_group, ''),
                                'Unallocated'),
                       sum(cost.amount)
                FROM daily_cost_history AS cost
                LEFT JOIN resources_current AS resource
                  ON lower(resource.resource_id) = cost.resource_id
                WHERE {period_where}
                GROUP BY resource.resource_group
                ORDER BY sum(cost.amount) DESC
                LIMIT 12
                """,
                period_params,
            ).fetchall()
            resource_rows = db.execute(
                f"""
                SELECT cost.resource_id,
                       COALESCE(NULLIF(resource.name, ''), cost.resource_id),
                       COALESCE(resource.resource_type, ''),
                       COALESCE(resource.resource_group, ''),
                       COALESCE(resource.region, ''),
                       cost.subscription_id,
                       sum(cost.amount)
                FROM daily_cost_history AS cost
                LEFT JOIN resources_current AS resource
                  ON lower(resource.resource_id) = cost.resource_id
                WHERE {period_where} AND cost.resource_id <> ''
                GROUP BY cost.resource_id, resource.name,
                         resource.resource_type, resource.resource_group,
                         resource.region, cost.subscription_id
                ORDER BY sum(cost.amount) DESC
                """,
                period_params,
            ).fetchall()
            region_rows = db.execute(
                f"""
                SELECT COALESCE(NULLIF(resource.region, ''), 'Global/unmapped'),
                       sum(cost.amount)
                FROM daily_cost_history AS cost
                LEFT JOIN resources_current AS resource
                  ON lower(resource.resource_id) = cost.resource_id
                WHERE {period_where}
                GROUP BY resource.region
                ORDER BY sum(cost.amount) DESC
                LIMIT 12
                """,
                period_params,
            ).fetchall()
            inventory_rows = db.execute(
                f"""
                SELECT COALESCE(NULLIF(resource.resource_type, ''),
                                'Unallocated'),
                       count(DISTINCT NULLIF(cost.resource_id, '')),
                       sum(cost.amount)
                FROM daily_cost_history AS cost
                LEFT JOIN resources_current AS resource
                  ON lower(resource.resource_id) = cost.resource_id
                WHERE {period_where}
                GROUP BY resource.resource_type
                ORDER BY sum(cost.amount) DESC
                LIMIT 20
                """,
                period_params,
            ).fetchall()
            tag_summary = db.execute(
                f"""
                SELECT
                    count(DISTINCT NULLIF(cost.resource_id, '')),
                    count(DISTINCT NULLIF(cost.resource_id, '')) FILTER (
                        WHERE json_array_length(
                            json_keys(resource.tags_json)
                        ) > 0
                    ),
                    COALESCE(sum(cost.amount) FILTER (
                        WHERE json_array_length(
                            json_keys(resource.tags_json)
                        ) = 0
                           OR resource.tags_json IS NULL
                    ), 0)
                FROM daily_cost_history AS cost
                LEFT JOIN resources_current AS resource
                  ON lower(resource.resource_id) = cost.resource_id
                WHERE {period_where}
                """,
                period_params,
            ).fetchone()
            facet_rows = {
                "currencies": db.execute(
                    """
                    SELECT DISTINCT currency FROM daily_cost_history
                    WHERE currency <> '' ORDER BY currency
                    """
                ).fetchall(),
                "subscriptions": db.execute(
                    """
                    WITH names AS (
                        SELECT subscription_id,
                               any_value(NULLIF(subscription_name, '')) AS name
                        FROM resources_current GROUP BY subscription_id
                    )
                    SELECT DISTINCT cost.subscription_id,
                           COALESCE(names.name, cost.subscription_id)
                    FROM daily_cost_history AS cost
                    LEFT JOIN names USING (subscription_id)
                    WHERE cost.cost_type = ?
                    ORDER BY 2, 1
                    """,
                    [cost_type],
                ).fetchall(),
                "services": db.execute(
                    """
                    SELECT DISTINCT service_name FROM daily_cost_history
                    WHERE service_name <> '' ORDER BY service_name
                    """
                ).fetchall(),
                "sources": db.execute(
                    f"""
                    SELECT DISTINCT source
                    FROM daily_cost_history AS cost
                    WHERE {period_where} AND source <> ''
                    ORDER BY source
                    """,
                    period_params,
                ).fetchall(),
            }
            coverage_rows = db.execute(
                """
                SELECT subscription_id, min(usage_date), max(usage_date),
                       count(*), max(observed_at)
                FROM daily_cost_history
                WHERE cost_type = ?
                GROUP BY subscription_id
                """,
                [cost_type],
            ).fetchall()
            forecast_conditions = list(scope_conditions)
            forecast_params = list(scope_params)
            if selected_currency:
                forecast_conditions.append("cost.currency = ?")
                forecast_params.append(selected_currency)
            if report_end:
                forecast_conditions.append("cost.usage_date <= ?")
                forecast_params.append(report_end)
            forecast_rows = db.execute(
                f"""
                SELECT cost.usage_date, sum(cost.amount)
                FROM daily_cost_history AS cost
                WHERE {' AND '.join(forecast_conditions)}
                GROUP BY cost.usage_date
                ORDER BY cost.usage_date
                """,
                forecast_params,
            ).fetchall()

            comparison_conditions = ["cost.cost_type IN ('ActualCost', 'AmortizedCost')"]
            comparison_params: list[Any] = []
            if subscription_id:
                comparison_conditions.append("cost.subscription_id = ?")
                comparison_params.append(subscription_id.lower())
            if service_name:
                comparison_conditions.append("cost.service_name = ?")
                comparison_params.append(service_name)
            if resource_id:
                comparison_conditions.append("cost.resource_id = ?")
                comparison_params.append(resource_id.lower())
            if selected_currency:
                comparison_conditions.append("cost.currency = ?")
                comparison_params.append(selected_currency)
            if report_start:
                comparison_conditions.append("cost.usage_date >= ?")
                comparison_params.append(report_start)
            if report_end:
                comparison_conditions.append("cost.usage_date <= ?")
                comparison_params.append(report_end)
            comparison_rows = db.execute(
                f"""
                SELECT cost.cost_type, sum(cost.amount)
                FROM daily_cost_history AS cost
                WHERE {' AND '.join(comparison_conditions)}
                GROUP BY cost.cost_type
                """,
                comparison_params,
            ).fetchall()

            # When the compared periods draw from materially different source
            # mixes (the mid-July FOCUS cutover, most visibly), service-level
            # movers partly reflect the sources' different groupings rather
            # than real cost movement: FOCUS books managed disks under
            # Virtual Machines and backup protection under Azure Site
            # Recovery, which Query-API history splits into Storage and
            # Backup. Pure renames are normalized at ingestion; grouping
            # differences cannot honestly be relabeled, so they are disclosed
            # whenever the FOCUS share of the two periods differs enough to
            # distort a service comparison. Resource-level movers are keyed
            # on resource_id, which is stable across sources.
            source_shift_note = ""
            if previous_start and previous_end and report_start and report_end:
                share_conditions = list(scope_conditions)
                share_params: list[Any] = list(scope_params)
                if selected_currency:
                    share_conditions.append("cost.currency = ?")
                    share_params.append(selected_currency)
                shares = db.execute(
                    f"""
                    SELECT
                        sum(abs(cost.amount)) FILTER (
                            WHERE cost.usage_date BETWEEN ? AND ?
                              AND cost.source = 'azure_focus_export'
                        ),
                        sum(abs(cost.amount)) FILTER (
                            WHERE cost.usage_date BETWEEN ? AND ?
                        ),
                        sum(abs(cost.amount)) FILTER (
                            WHERE cost.usage_date BETWEEN ? AND ?
                              AND cost.source = 'azure_focus_export'
                        ),
                        sum(abs(cost.amount)) FILTER (
                            WHERE cost.usage_date BETWEEN ? AND ?
                        )
                    FROM daily_cost_history AS cost
                    WHERE {' AND '.join(share_conditions)}
                    """,
                    [
                        report_start, report_end,
                        report_start, report_end,
                        previous_start, previous_end,
                        previous_start, previous_end,
                        *share_params,
                    ],
                ).fetchone()
                current_share = (
                    float(shares[0] or 0) / float(shares[1])
                    if shares[1] else 0.0
                )
                previous_share = (
                    float(shares[2] or 0) / float(shares[3])
                    if shares[3] else 0.0
                )
                if abs(current_share - previous_share) > 0.25:
                    source_shift_note = (
                        "The compared periods draw from different cost "
                        f"sources (FOCUS covers {current_share:.0%} of the "
                        f"current period but {previous_share:.0%} of the "
                        "previous one). FOCUS groups some charges under "
                        "different services than Query-API history (managed "
                        "disks under Virtual Machines, backup under Azure "
                        "Site Recovery), so service-level movers across "
                        "this boundary partly reflect labeling, not real "
                        "change. Resource-level movers are unaffected."
                    )

            def movers(
                key_sql: str,
                label_sql: str,
                extra_condition: str = "",
            ) -> list[dict[str, Any]]:
                if not previous_start or not previous_end or not report_end:
                    return []
                mover_conditions = list(scope_conditions)
                mover_params = list(scope_params)
                if selected_currency:
                    mover_conditions.append("cost.currency = ?")
                    mover_params.append(selected_currency)
                mover_conditions.extend(
                    ["cost.usage_date >= ?", "cost.usage_date <= ?"]
                )
                mover_params.extend([previous_start, report_end])
                if extra_condition:
                    mover_conditions.append(extra_condition)
                rows = db.execute(
                    f"""
                    SELECT {key_sql} AS item_key, {label_sql} AS item_label,
                           COALESCE(sum(cost.amount) FILTER (
                               WHERE cost.usage_date >= ?
                                 AND cost.usage_date <= ?
                           ), 0) AS current_cost,
                           COALESCE(sum(cost.amount) FILTER (
                               WHERE cost.usage_date >= ?
                                 AND cost.usage_date <= ?
                           ), 0) AS previous_cost
                    FROM daily_cost_history AS cost
                    LEFT JOIN resources_current AS resource
                      ON lower(resource.resource_id) = cost.resource_id
                    WHERE {' AND '.join(mover_conditions)}
                    GROUP BY item_key, item_label
                    """,
                    [
                        report_start,
                        report_end,
                        previous_start,
                        previous_end,
                        *mover_params,
                    ],
                ).fetchall()
                values = []
                for key, label, current, previous in rows:
                    delta = float(current or 0) - float(previous or 0)
                    values.append(
                        {
                            "id": key or "",
                            "name": label or key or "Unallocated",
                            "current": round(float(current or 0), 2),
                            "previous": round(float(previous or 0), 2),
                            "change": round(delta, 2),
                            "changePercent": round(
                                delta / float(previous) * 100,
                                1,
                            )
                            if previous
                            else None,
                        }
                    )
                return sorted(
                    values,
                    key=lambda item: abs(item["change"]),
                    reverse=True,
                )[:15]

            mover_values = {
                "subscriptions": movers(
                    "cost.subscription_id",
                    "cost.subscription_id",
                ),
                "services": movers(
                    "cost.service_name",
                    "COALESCE(NULLIF(cost.service_name, ''), 'Unallocated')",
                ),
                "resources": movers(
                    "cost.resource_id",
                    "COALESCE(NULLIF(resource.name, ''), cost.resource_id)",
                    "cost.resource_id <> ''",
                ),
            }

        cumulative = 0.0
        daily = []
        for observed, amount in daily_rows:
            cumulative += float(amount or 0)
            daily.append(
                {
                    "date": observed.isoformat(),
                    "amount": round(float(amount or 0), 2),
                    "cumulative": round(cumulative, 2),
                }
            )
        forecast = forecast_daily_cost(
            {row[0]: float(row[1] or 0) for row in forecast_rows},
            horizon_days=forecast_horizon_days,
            latency_days=forecast_latency_days,
        )
        history_status = self.cost_history_status()
        latest_scope = {
            (item["subscriptionId"], item["costType"]): item
            for item in history_status["scopes"]
        }
        coverage_by_subscription = {
            row[0]: {
                "availableStart": row[1].isoformat() if row[1] else None,
                "availableEnd": row[2].isoformat() if row[2] else None,
                "rowCount": row[3],
                "observedAt": row[4].isoformat() if row[4] else None,
            }
            for row in coverage_rows
        }
        coverage_scopes = []
        for configured in configured_subscriptions:
            available_scope = coverage_by_subscription.get(configured["id"])
            attempted_scope = latest_scope.get((configured["id"], cost_type), {})
            available_start = (available_scope or {}).get("availableStart")
            available_end = (available_scope or {}).get("availableEnd")
            period_complete = bool(available_scope) and (
                not report_start
                or (
                    bool(available_start)
                    and available_start <= report_start.isoformat()
                )
            ) and (
                not report_end
                or (
                    bool(available_end)
                    and available_end >= report_end.isoformat()
                )
            )
            # A subscription whose latest collection SUCCEEDED with zero
            # rows is covered-and-empty, not unavailable: Azure reports
            # nothing billed there (an empty subscription reports zero
            # resources and zero cost from both APIs).
            # Without this it permanently reads as "1 configured scope
            # unavailable" and every Ask Flux answer carries a partial-
            # coverage warning about a subscription with nothing to cover.
            succeeded_empty = (
                available_scope is None
                and attempted_scope.get("status") == "succeeded"
            )
            coverage_scopes.append(
                {
                    **configured,
                    "available": available_scope is not None or succeeded_empty,
                    "complete": period_complete or succeeded_empty,
                    "status": attempted_scope.get("status") or (
                        "available" if available_scope else "not_collected"
                    ),
                    "retainedLastGood": attempted_scope.get(
                        "retainedLastGood", False
                    ),
                    "statusCode": attempted_scope.get("statusCode"),
                    "message": attempted_scope.get("message", ""),
                    **(available_scope or {
                        "availableStart": None,
                        "availableEnd": None,
                        "rowCount": 0,
                        "observedAt": None,
                    }),
                }
            )
        # Day-level ingestion coverage for the reported window. Only computed
        # for whole-subscription views: under a service or resource filter a
        # day with no rows legitimately means no spend, not missing data.
        coverage = None
        if (
            report_start
            and report_end
            and configured_subscriptions
            and not service_name
            and not resource_id
        ):
            scope_ids = (
                [subscription_id.lower()]
                if subscription_id
                else [item["id"] for item in configured_subscriptions]
            )
            window_days = (report_end - report_start).days + 1
            expected_scope_days = window_days * len(scope_ids)
            placeholders = ", ".join("?" for _ in scope_ids)
            with self.connect(read_only=True) as coverage_db:
                ingested_scope_days = coverage_db.execute(
                    f"""
                    SELECT count(*) FROM (
                        SELECT DISTINCT subscription_id, usage_date
                        FROM daily_cost_history
                        WHERE cost_type = ?
                          AND usage_date BETWEEN ? AND ?
                          AND subscription_id IN ({placeholders})
                    )
                    """,
                    [cost_type, report_start, report_end, *scope_ids],
                ).fetchone()[0]
            ingested_scope_days = int(ingested_scope_days or 0)
            coverage = {
                "expectedScopeDays": expected_scope_days,
                "ingestedScopeDays": ingested_scope_days,
                "coveragePercent": (
                    round(
                        ingested_scope_days / expected_scope_days * 100, 1
                    )
                    if expected_scope_days
                    else None
                ),
            }
            if expected_scope_days and (
                ingested_scope_days < expected_scope_days
            ):
                gap_note = (
                    f"Ingested {ingested_scope_days} of "
                    f"{expected_scope_days} subscription-days for this "
                    "window; totals may understate actual spend while the "
                    "collector backfills the gap."
                )
                period_note = (
                    f"{period_note} {gap_note}".strip()
                    if period_note
                    else gap_note
                )
        subscription_facets = {
            item["id"]: item["name"] for item in configured_subscriptions
        }
        subscription_facets.update(
            {row[0]: row[1] for row in facet_rows["subscriptions"]}
        )
        return {
            "period": {
                "availableStart": minimum_date.isoformat()
                if minimum_date
                else None,
                "availableEnd": maximum_date.isoformat()
                if maximum_date
                else None,
                "start": report_start.isoformat() if report_start else None,
                "end": report_end.isoformat() if report_end else None,
                "previousStart": previous_start.isoformat()
                if previous_start
                else None,
                "previousEnd": previous_end.isoformat()
                if previous_end
                else None,
                "note": period_note or None,
                "coverage": coverage,
            },
            "summary": {
                "costType": cost_type,
                "currency": selected_currency,
                "currencyCount": available[2] or 0,
                "totalCost": round(current_total, 2),
                "previousCost": round(previous_total, 2),
                "changeAmount": round(current_total - previous_total, 2),
                "changePercent": round(
                    (current_total - previous_total)
                    / previous_total
                    * 100,
                    1,
                )
                if previous_total
                else None,
                "averageDailyCost": round(
                    current_total / len(daily_rows),
                    2,
                )
                if daily_rows
                else None,
                "resourceCount": tag_summary[0] or 0,
                "taggedResourceCount": tag_summary[1] or 0,
                "untaggedCost": round(tag_summary[2] or 0, 2),
            },
            "costTypeComparison": {
                row[0]: round(float(row[1] or 0), 2)
                for row in comparison_rows
            },
            "topMovers": mover_values,
            "daily": daily,
            "bySubscription": [
                {"id": row[0], "name": row[1], "value": round(row[2], 2)}
                for row in subscription_rows
            ],
            "byService": [
                {"name": row[0], "value": round(row[1], 2)}
                for row in service_rows
            ],
            "byResourceGroup": [
                {"name": row[0], "value": round(row[1], 2)}
                for row in resource_group_rows
            ],
            "byRegion": [
                {"name": row[0], "value": round(row[1], 2)}
                for row in region_rows
            ],
            "inventory": [
                {
                    "resourceType": row[0],
                    "resourceCount": row[1],
                    "cost": round(row[2], 2),
                    "costPerResource": round(row[2] / row[1], 2)
                    if row[1]
                    else None,
                }
                for row in inventory_rows
            ],
            "resources": [
                {
                    "resourceId": row[0],
                    "resourceName": row[1],
                    "resourceType": row[2],
                    "resourceGroup": row[3],
                    "region": row[4],
                    "subscriptionId": row[5],
                    "cost": round(row[6], 2),
                }
                for row in resource_rows
            ],
            "forecast": forecast,
            "budgetVariance": {
                "status": "blocked_missing_targets",
                "variance": None,
                "reason": (
                    "No approved organizational budget targets are configured; "
                    "Flux will not infer a budget."
                ),
            },
            "dataCoverage": {
                "configuredScopes": len(coverage_scopes),
                "availableScopes": sum(
                    1 for item in coverage_scopes if item["available"]
                ),
                "completeScopes": sum(
                    1 for item in coverage_scopes if item["complete"]
                ),
                "complete": bool(coverage_scopes) and all(
                    item["complete"] for item in coverage_scopes
                ),
                "scopes": coverage_scopes,
            },
            "facets": {
                "currencies": [row[0] for row in facet_rows["currencies"]],
                "subscriptions": [
                    {"id": item[0], "name": item[1]}
                    for item in sorted(
                        subscription_facets.items(),
                        key=lambda value: (value[1], value[0]),
                    )
                ],
                "services": [row[0] for row in facet_rows["services"]],
            },
            "lineage": {
                "source": "governed_daily_cost_history",
                "sources": [row[0] for row in facet_rows["sources"]],
                "grain": "daily resource and service",
                "toolkitReference": "Microsoft FinOps Toolkit v14 Cost summary",
                "limitations": [
                    "This report is daily aggregate history; use the governed "
                    "FOCUS report for charge-level drivers and export coverage.",
                    "Usage quantity, purchases, invoice reconciliation, and "
                    "contracted-price savings are not inferred.",
                    *([source_shift_note] if source_shift_note else []),
                ],
            },
        }

    def latest_sync(
        self, _operational_db: Any | None = None
    ) -> dict[str, Any] | None:
        # Fetch the latest sync run and its source runs in one query/connection
        # rather than two sequential operational round-trips. A LEFT JOIN keeps
        # the sync row when no source runs exist yet; the client groups the
        # joined rows back into the original nested shape.
        with self._optional_operational_connect(_operational_db) as db:
            rows = db.execute(
                """
                WITH latest AS (
                    SELECT *,
                        row_number() OVER (
                            ORDER BY started_at DESC, id DESC
                        ) AS rank
                    FROM sync_runs
                )
                SELECT
                    latest.id, latest.provider, latest.started_at,
                    latest.completed_at, latest.status, latest.resource_count,
                    latest.message, latest.trigger, latest.stage,
                    latest.stage_message, latest.claimed_at,
                    latest.requested_sources_json,
                    sources.source, sources.scope_id, sources.started_at,
                    sources.completed_at, sources.status, sources.attempt_count,
                    sources.row_count, sources.retained_last_good,
                    sources.message, sources.last_attempt_at,
                    sources.status_code, sources.retry_after_seconds,
                    sources.next_retry_at
                FROM latest
                LEFT JOIN sync_source_runs AS sources
                  ON sources.sync_id = latest.id
                WHERE latest.rank = 1
                ORDER BY sources.source, sources.scope_id
                """
            ).fetchall()
        if not rows or rows[0][0] is None:
            return None
        head = rows[0]
        source_runs = []
        for item in rows:
            if item[12] is None:
                continue
            source_runs.append(
                {
                    "source": item[12],
                    "scopeId": item[13],
                    "startedAt": item[14].isoformat(),
                    "completedAt": item[15].isoformat() if item[15] else None,
                    "status": item[16],
                    "attemptCount": item[17],
                    "rowCount": item[18],
                    "retainedLastGood": item[19],
                    "message": item[20],
                    "lastAttemptAt": (
                        item[21].isoformat() if item[21] else None
                    ),
                    "statusCode": item[22],
                    "retryAfterSeconds": item[23],
                    "nextRetryAt": (
                        item[24].isoformat() if item[24] else None
                    ),
                }
            )
        return {
            "id": head[0],
            "provider": head[1],
            "startedAt": head[2].isoformat(),
            "completedAt": head[3].isoformat() if head[3] else None,
            "status": head[4],
            "resourceCount": head[5],
            "message": head[6],
            "trigger": head[7] or "manual",
            "stage": head[8] or "",
            "stageMessage": head[9] or head[6],
            "claimedAt": head[10].isoformat() if head[10] else None,
            "requestedSources": json.loads(head[11] or "[]"),
            "sourceRuns": source_runs,
        }

    def source_freshness(
        self, _operational_db: Any | None = None
    ) -> list[dict[str, Any]]:
        labels = {
            "AzureResourceGraph": "Azure inventory",
            "AzureAdvisor": "Azure Advisor",
            "FluxIntelligence": "Flux Signals",
            "AzurePolicy": "Azure Policy",
            "ActualCost": "Actual cost",
            "AmortizedCost": "Amortized cost",
            "DailyActualCost": "Daily actual cost",
            "DailyAmortizedCost": "Daily amortized cost",
            "CostAnomalies": "Cost anomalies",
            "CommitmentCoverage": "Commitment coverage",
            "AzureRetailPrices": "Azure retail prices",
            "FocusCost": "FOCUS cost export",
            "Commitments": "Commitments inventory",
            "PriceSheet": "Negotiated price sheet",
            "azure_monitor": "Azure Monitor",
            "ama_log_analytics": "AMA guest metrics",
            "logicmonitor": "LogicMonitor",
        }
        metadata = {
            "AzureResourceGraph": (30, 24, "Daily at 10:00 UTC"),
            "FluxIntelligence": (30, 24, "Daily at 10:30 UTC"),
            "AzurePolicy": (30, 24, "Daily at 10:00 UTC"),
            "ActualCost": (30, 24, "Daily at 11:00 UTC"),
            "AmortizedCost": (30, 24, "Daily at 11:00 UTC"),
            "DailyActualCost": (36, 24, "Daily at 12:30 UTC"),
            "DailyAmortizedCost": (36, 24, "Daily at 12:30 UTC"),
            "CostAnomalies": (36, 24, "Daily after cost history"),
            "CommitmentCoverage": (30, 24, "Daily at 11:00 UTC"),
            "AzureRetailPrices": (
                30, 6, "Every 6 hours; rates refresh daily"
            ),
            "Commitments": (30, 24, "Daily at 09:20 UTC"),
            "PriceSheet": (30, 24, "Daily at 08:50 UTC"),
            "AzureAdvisor": (12, 6, "Every 6 hours"),
            "azure_monitor": (12, 6, "Every 6 hours"),
            "ama_log_analytics": (12, 6, "Every 6 hours"),
            "logicmonitor": (12, 6, "Every 6 hours"),
        }
        # Enabled is the right gate, not enabled-and-required: an estate with
        # optional FOCUS ingestion still runs flux-focus-cost every 6 hours,
        # and without metadata the source fell back to a 24h cadence and the
        # midnight-only default schedule below.
        if self.focus_cost_enabled:
            metadata["FocusCost"] = (12, 6, "Every 6 hours")
        with self.connect(read_only=True) as db:
            rows = db.execute(
                """
                WITH current AS (
                    SELECT *,
                        row_number() OVER (
                            PARTITION BY source, scope_id
                            ORDER BY observed_at DESC, snapshot_id DESC
                        ) AS rank
                    FROM source_sync_state
                )
                SELECT source, max(observed_at), sum(row_count)
                FROM current
                WHERE rank = 1
                GROUP BY source
                ORDER BY source
                """
            ).fetchall()
        with self._optional_operational_connect(_operational_db) as db:
            latest_status = (
                "(array_agg(status ORDER BY started_at DESC))[1]"
                if self.operational_backend == "postgres"
                else "arg_max(status, started_at)"
            )
            latest_message = (
                "(array_agg(message ORDER BY started_at DESC))[1]"
                if self.operational_backend == "postgres"
                else "arg_max(message, started_at)"
            )
            attempts = db.execute(
                f"""
                WITH ranked AS (
                    SELECT *,
                        row_number() OVER (
                            PARTITION BY source, scope_id
                            ORDER BY started_at DESC, sync_id DESC
                        ) AS rank
                    FROM sync_source_runs
                )
                SELECT source, max(started_at),
                       count(*),
                       count(*) FILTER (WHERE status = 'succeeded'),
                       bool_or(retained_last_good),
                       {latest_status},
                       {latest_message}
                FROM ranked
                WHERE rank = 1
                GROUP BY source
                """
            ).fetchall()
        with self.connect(read_only=True) as db:
            telemetry = db.execute(
                """
                WITH latest AS (
                    SELECT *, row_number() OVER (
                        PARTITION BY source ORDER BY started_at DESC
                    ) AS rank
                    FROM telemetry_runs
                ), successful AS (
                    SELECT source, max(completed_at) AS last_success_at
                    FROM telemetry_runs
                    WHERE status = 'succeeded'
                    GROUP BY source
                )
                SELECT latest.source, latest.started_at, latest.completed_at,
                       latest.status, latest.processed_count, latest.message,
                       successful.last_success_at
                FROM latest
                LEFT JOIN successful USING (source)
                WHERE latest.rank = 1
                """
            ).fetchall()
        attempt_by_source = {
            item[0]: {
                "lastAttemptAt": item[1].isoformat(),
                "scopeTotal": item[2],
                "scopeSucceeded": item[3],
                "retainedLastGood": item[4],
                "lastAttemptStatus": item[5],
                "lastAttemptMessage": item[6],
            }
            for item in attempts
        }
        values = []
        now = utc_now()

        # Grace windows to absorb normal scheduling jitter (hours).
        _GRACE_DAILY = 4
        _GRACE_INTERVAL = 2

        def next_scheduled(source: str) -> datetime:
            daily = {
                "AzureResourceGraph": (10, 0),
                "AzurePolicy": (10, 0),
                "FluxIntelligence": (10, 30),
                "ActualCost": (11, 0),
                "AmortizedCost": (11, 0),
                "CommitmentCoverage": (11, 0),
                "DailyActualCost": (12, 30),
                "DailyAmortizedCost": (12, 30),
                "CostAnomalies": (12, 30),
                "PriceSheet": (8, 50),
                "Commitments": (9, 20),
            }
            if source in daily:
                hour, minute = daily[source]
                candidate = now.replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
                return (
                    candidate
                    if candidate > now
                    else candidate + timedelta(days=1)
                )
            interval = {
                "AzureAdvisor": ([0, 6, 12, 18], 45),
                "azure_monitor": ([0, 6, 12, 18], 15),
                "ama_log_analytics": ([0, 6, 12, 18], 15),
                "logicmonitor": ([0, 6, 12, 18], 10),
                "AzureRetailPrices": ([1, 7, 13, 19], 15),
                # flux-focus-cost: "0 0 */6 * * *". Missing from this map,
                # FocusCost fell to the midnight-only default, so
                # last-expected sat hours in the future and the staleness
                # check flagged fresh 6-hourly data as stale for most of
                # every day -- a permanent warning no schedule could satisfy.
                "FocusCost": ([0, 6, 12, 18], 0),
            }
            hours, minute = interval.get(source, ([0], 0))
            for hour in hours:
                candidate = now.replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
                if candidate > now:
                    return candidate
            return now.replace(
                hour=hours[0], minute=minute, second=0, microsecond=0
            ) + timedelta(days=1)

        def is_stale(source: str, observed_at: datetime | None) -> bool:
            """Schedule-aware staleness: only stale when the most recent
            scheduled run has been missed plus a grace window."""
            if observed_at is None:
                return True
            stale_after, cadence, _ = metadata.get(source, (24, 24, ""))
            # Hard backstop: if data is older than stale_after, always stale.
            # This handles sources with no schedule or unusual cadence.
            if (now - observed_at).total_seconds() / 3600 > stale_after:
                return True
            # Schedule-aware check: compare against the LAST expected run time.
            next_expected = next_scheduled(source)
            last_expected = next_expected - timedelta(hours=cadence)
            grace = _GRACE_DAILY if cadence >= 24 else _GRACE_INTERVAL
            return observed_at < last_expected - timedelta(hours=grace)

        for row in rows:
            if row[0] == "FocusCost" and not self.focus_cost_enabled:
                continue
            stale_after, _cadence, schedule = metadata.get(
                row[0], (24, 24, "Scheduled")
            )
            age_hours = (
                (now - row[1]).total_seconds() / 3600 if row[1] else None
            )
            values.append({
                "source": row[0],
                "label": labels.get(row[0], row[0]),
                "observedAt": row[1].isoformat() if row[1] else None,
                "rowCount": row[2] or 0,
                "stale": is_stale(row[0], row[1]),
                "ageHours": round(age_hours, 1) if age_hours is not None else None,
                "staleAfterHours": stale_after,
                "schedule": schedule,
                "nextExpectedAt": next_scheduled(row[0]).isoformat(),
                **attempt_by_source.get(row[0], {}),
            })
        for row in telemetry:
            stale_after, _cadence, schedule = metadata.get(
                row[0], (12, 6, "Every 6 hours")
            )
            observed_at = row[6]
            age_hours = (
                (now - observed_at).total_seconds() / 3600
                if observed_at else None
            )
            values.append(
                {
                    "source": row[0],
                    "label": labels.get(row[0], row[0]),
                    "observedAt": observed_at.isoformat() if observed_at else None,
                    "rowCount": row[4] or 0,
                    "stale": is_stale(row[0], observed_at),
                    "ageHours": round(age_hours, 1) if age_hours is not None else None,
                    "staleAfterHours": stale_after,
                    "schedule": schedule,
                    "nextExpectedAt": next_scheduled(row[0]).isoformat(),
                    "lastAttemptAt": row[1].isoformat(),
                    "lastAttemptStatus": row[3],
                    "lastAttemptMessage": row[5],
                    "scopeTotal": 1,
                    "scopeSucceeded": 1 if row[3] == "succeeded" else 0,
                    "retainedLastGood": row[3] != "succeeded" and bool(observed_at),
                }
            )
        present = {item["source"] for item in values}
        for source, (stale_after, _cadence, schedule) in metadata.items():
            if source in present:
                continue
            # Optional FOCUS stays hidden until data exists; it is in
            # metadata regardless so an estate WITH data gets the correct
            # 6-hourly schedule instead of the midnight default.
            if source == "FocusCost" and not self.focus_cost_required:
                continue
            # AMA guest metrics stay hidden until the workspace is
            # configured and the first collection lands; estates without
            # the AMA deployment should not carry a never-synced row.
            if source == "ama_log_analytics":
                continue
            # Unprovisioned sources (never synced) should not trigger the
            # stale/degraded warning banner. They show as "never synced" in
            # the integrations detail view instead.
            values.append(
                {
                    "source": source,
                    "label": labels.get(source, source),
                    "observedAt": None,
                    "rowCount": 0,
                    "stale": False,
                    "ageHours": None,
                    "staleAfterHours": stale_after,
                    "schedule": schedule,
                    "nextExpectedAt": next_scheduled(source).isoformat(),
                    **attempt_by_source.get(source, {}),
                }
            )
        for item in values:
            if (
                item.get("lastAttemptStatus") == "failed"
                or item.get("scopeSucceeded", 0) < item.get("scopeTotal", 0)
            ):
                item["health"] = "degraded"
            elif item["stale"]:
                item["health"] = "stale"
            else:
                item["health"] = "healthy"
        order = {name: index for index, name in enumerate(labels)}
        return sorted(values, key=lambda item: order.get(item["source"], 99))

    def recommendation_quality(self) -> dict[str, Any]:
        """Reconcile Advisor storage, identity, and actionability counts."""
        advisor = self.opportunities(
            source="azure_advisor",
            include_governance=True,
            limit=1,
        )
        with self.connect(read_only=True) as db:
            row = db.execute(
                """
                WITH current AS (
                    SELECT *
                    FROM advisor_recommendations_current
                )
                SELECT
                    count(*) AS recommendations,
                    count(DISTINCT recommendation_id) AS unique_ids,
                    count(*) FILTER (
                        WHERE current.resource_id = concat(
                            '/subscriptions/', lower(current.subscription_id)
                        )
                    ) AS subscription_scoped,
                    count(*) FILTER (
                        WHERE current.resource_id <> concat(
                            '/subscriptions/', lower(current.subscription_id)
                        )
                          AND resource.resource_id IS NOT NULL
                    ) AS resolved_resources,
                    count(*) FILTER (
                        WHERE current.resource_id <> concat(
                            '/subscriptions/', lower(current.subscription_id)
                        )
                          AND resource.resource_id IS NULL
                    ) AS unresolved_resources,
                    count(DISTINCT concat_ws(
                        chr(31),
                        lower(current.subscription_id),
                        lower(current.resource_id),
                        lower(current.category),
                        lower(current.recommendation_type_id),
                        lower(current.problem),
                        lower(current.solution),
                        lower(current.current_sku),
                        lower(current.recommended_sku),
                        lower(COALESCE(
                            json_extract_string(
                                current.raw_json,
                                '$._fluxActionContext'
                            ),
                            ''
                        ))
                    )) AS semantic_actions
                FROM current
                LEFT JOIN resources_current AS resource
                  ON resource.resource_id = current.resource_id
                """
            ).fetchone()
            estate_resources = db.execute(
                "SELECT count(*) FROM resources_current"
            ).fetchone()[0]
        recommendations = int(row[0] or 0)
        unique_ids = int(row[1] or 0)
        semantic_actions = int(row[5] or 0)
        unresolved = int(row[4] or 0)
        duplicate_ids = max(0, recommendations - unique_ids)
        semantic_duplicates = max(0, recommendations - semantic_actions)
        portfolio = advisor["summary"]["portfolio"]
        checks = [
            {
                "name": "Recommendation IDs",
                "status": "passed" if duplicate_ids == 0 else "failed",
                "value": duplicate_ids,
                "message": (
                    "No duplicate active recommendation IDs."
                    if duplicate_ids == 0
                    else f"{duplicate_ids:,} duplicate active IDs detected."
                ),
            },
            {
                "name": "Semantic actions",
                "status": "passed" if semantic_duplicates == 0 else "failed",
                "value": semantic_duplicates,
                "message": (
                    "No semantically repeated active actions."
                    if semantic_duplicates == 0
                    else (
                        f"{semantic_duplicates:,} semantically repeated "
                        "active actions detected."
                    )
                ),
            },
            {
                "name": "Resource identity",
                "status": "passed" if unresolved == 0 else "review",
                "value": unresolved,
                "message": (
                    "Every resource-scoped recommendation resolves to inventory."
                    if unresolved == 0
                    else (
                        f"{unresolved:,} resource-scoped recommendations do "
                        "not resolve to current inventory."
                    )
                ),
            },
        ]
        return {
            "status": (
                "failed"
                if any(item["status"] == "failed" for item in checks)
                else "review"
                if any(item["status"] == "review" for item in checks)
                else "healthy"
            ),
            "asOf": utc_now().isoformat(),
            "storedActive": recommendations,
            "uniqueIds": unique_ids,
            "semanticActions": semantic_actions,
            "duplicateIds": duplicate_ids,
            "semanticDuplicates": semantic_duplicates,
            "subscriptionScoped": int(row[2] or 0),
            "resolvedResources": int(row[3] or 0),
            "unresolvedResources": unresolved,
            "estateResources": int(estate_resources or 0),
            "recommendationsPerResource": round(
                recommendations / estate_resources, 3
            )
            if estate_resources
            else 0,
            "portfolio": portfolio,
            "checks": checks,
            "method": (
                "Active Advisor rows are reconciled by source ID, semantic "
                "action, current resource identity, and governed actionability."
            ),
        }

    def intelligence_quality_status(
        self,
        retention_days: int = 30,
        slow_request_ms: int = 20_000,
    ) -> dict[str, Any]:
        """Summarize retained response quality and stage-level performance."""
        window = max(1, retention_days)
        with self.operational_connect(read_only=True) as db:
            rows = db.execute(
                """
                SELECT
                    usage.status,
                    usage.latency_ms,
                    usage.client_end_to_end_ms,
                    usage.model_latency_ms,
                    usage.governed_tool_latency_ms,
                    usage.database_latency_ms,
                    usage.validation_latency_ms,
                    usage.application_latency_ms,
                    usage.transport_ingress_ms,
                    usage.client_render_ms,
                    usage.feedback_rating,
                    transcript.response_json
                FROM intelligence_usage_events AS usage
                LEFT JOIN intelligence_transcript_events AS transcript
                  USING (request_id)
                WHERE usage.occurred_at
                    >= current_timestamp - (? * INTERVAL '1 day')
                ORDER BY usage.occurred_at DESC
                """,
                [window],
            ).fetchall()
        modes: dict[str, int] = {}
        flagged = 0
        structured_contract_failures = 0
        helpful = 0
        not_helpful = 0
        bottlenecks: dict[str, int] = {}
        quality_flags: dict[str, int] = {}
        quality_scores: list[int] = []
        regression_failures = 0
        slow = 0
        for row in rows:
            response = json.loads(row[11]) if row[11] else {}
            performance = response.get("performance") or {}
            mode = str(performance.get("responseMode") or "unknown")
            modes[mode] = modes.get(mode, 0) + 1
            limitations = response.get("limitations") or []
            if any(
                str(item).lower().startswith("review flag:")
                for item in limitations
            ):
                flagged += 1
            if mode in {"plain_text", "plain_text_review"}:
                structured_contract_failures += 1
            assessment = response.get("quality") or {}
            assessment_score = assessment.get("score")
            if isinstance(assessment_score, (int, float)):
                quality_scores.append(int(assessment_score))
                if assessment.get("status") != "pass":
                    regression_failures += 1
                for flag in assessment.get("flags") or []:
                    name = str(flag)
                    quality_flags[name] = quality_flags.get(name, 0) + 1
            if row[10] == "helpful":
                helpful += 1
            elif row[10] == "not_helpful":
                not_helpful += 1
            duration = int(row[2] or row[1] or 0)
            if duration >= slow_request_ms:
                slow += 1
            stages = {
                "model": int(row[3] or 0),
                "governed_tools": int(row[4] or 0),
                "database": int(row[5] or 0),
                "validation": int(row[6] or 0),
                "application": int(row[7] or 0),
                "network_ingress": int(row[8] or 0),
                "browser_render": int(row[9] or 0),
            }
            if duration and any(stages.values()):
                bottleneck = max(stages, key=stages.get)
                bottlenecks[bottleneck] = bottlenecks.get(bottleneck, 0) + 1
        request_count = len(rows)
        reviewed = helpful + not_helpful
        health = (
            "warming_up"
            if request_count < 5
            else "degraded"
            if structured_contract_failures > 0
            or regression_failures > 0
            or (reviewed >= 3 and not_helpful / reviewed > 0.25)
            else "healthy"
        )
        return {
            "status": health,
            "retentionDays": window,
            "slowRequestThresholdMs": slow_request_ms,
            "requestCount": request_count,
            "slowRequestCount": slow,
            "flaggedForReviewCount": flagged,
            "structuredContractFailureCount": structured_contract_failures,
            "assessedCount": len(quality_scores),
            "averageScore": (
                round(sum(quality_scores) / len(quality_scores), 1)
                if quality_scores
                else None
            ),
            "regressionFailureCount": regression_failures,
            "qualityFlags": [
                {"flag": name, "count": count}
                for name, count in sorted(
                    quality_flags.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ],
            "helpfulCount": helpful,
            "notHelpfulCount": not_helpful,
            "unratedCount": max(0, request_count - reviewed),
            "helpfulPercent": round(helpful / reviewed * 100, 1)
            if reviewed
            else None,
            "responseModes": [
                {"mode": name, "count": count}
                for name, count in sorted(modes.items())
            ],
            "bottlenecks": [
                {"stage": name, "count": count}
                for name, count in sorted(
                    bottlenecks.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ],
            "openItem": (
                "Stakeholder-authored evaluation questions and acceptance "
                "thresholds remain outstanding."
            ),
        }

    def database_health(self) -> dict[str, Any]:
        """Report both storage engines without opening the analytical file.

        Deliberately filesystem-only for DuckDB. The web process must never
        open the mutable database: doing so is what took production down on
        2026-07-29, and an admin health probe is exactly the kind of
        well-intentioned read that would reintroduce it. Size, mtime and the
        writer-lease flag all come from stat()/lock inspection, so this stays
        safe to call while a collector is mid-write.
        """
        now = utc_now()
        analytical: dict[str, Any] = {
            "engine": "duckdb",
            "path": str(self.path),
            "exists": False,
            "sizeBytes": 0,
            "modifiedAt": None,
            "ageSeconds": None,
            "writerLeaseHeld": None,
            "note": (
                "Reported from filesystem metadata only; the web process "
                "never opens the mutable analytical database."
            ),
        }
        try:
            stat = self.path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            analytical.update(
                {
                    "exists": True,
                    "sizeBytes": int(stat.st_size),
                    "modifiedAt": modified.isoformat(),
                    "ageSeconds": round((now - modified).total_seconds(), 1),
                }
            )
        except OSError as error:
            analytical["error"] = f"{type(error).__name__}: {error}"
        try:
            # is_locked reflects this process only; the on-disk lock file is
            # the cross-process signal that a writer currently holds the lease.
            lock_path = Path(str(self.path) + ".writer.lock")
            analytical["writerLeaseHeld"] = (
                self._writer_lock.is_locked or lock_path.exists()
            )
        except Exception:
            analytical["writerLeaseHeld"] = None

        control: dict[str, Any] = {
            "engine": self._operational.backend,
            "reachable": False,
            "latencyMs": None,
        }
        started = time.monotonic()
        try:
            with self.operational_connect(read_only=True) as db:
                db.execute("SELECT 1").fetchone()
            control["reachable"] = True
            control["latencyMs"] = round((time.monotonic() - started) * 1000, 1)
        except Exception as error:
            control["error"] = f"{type(error).__name__}: {error}"

        return {
            "generatedAt": now.isoformat(),
            "controlPlane": control,
            "analytical": analytical,
        }

    def configuration_audit(self, limit: int = 25) -> list[dict[str, Any]]:
        """Who last changed each governed configuration surface.

        The allocation, budget and AI-config tables already persist
        updated_by/updated_at but nothing ever surfaced them, so cost
        governance changes were effectively unattributed.
        """
        entries: list[dict[str, Any]] = []
        with self.operational_connect(read_only=True) as db:
            def collect(sql: str, label: str, scope_is_column: bool) -> None:
                try:
                    for row in db.execute(sql).fetchall():
                        entries.append(
                            {
                                "surface": label,
                                "scope": str(row[0] or "") if scope_is_column else "",
                                "updatedBy": str(row[1] or "") or "Unknown",
                                "updatedAt": (
                                    row[2].isoformat()
                                    if isinstance(row[2], datetime)
                                    else (str(row[2]) if row[2] else None)
                                ),
                            }
                        )
                except Exception:
                    # A surface that has never been written has no row; an
                    # audit view must not fail because of that.
                    return

            collect(
                """
                SELECT '', '', updated_at FROM allocation_config
                WHERE id = 'default'
                """,
                "Cost allocation",
                False,
            )
            collect(
                """
                SELECT scope_type || ':' || scope_id, updated_by, updated_at
                FROM budget_targets ORDER BY updated_at DESC
                """,
                "Budget target",
                True,
            )
            collect(
                """
                SELECT provider, updated_by, updated_at
                FROM ai_intelligence_config WHERE id = 'default'
                """,
                "Ask Flux AI",
                True,
            )
        entries = [item for item in entries if item["updatedAt"]]
        entries.sort(key=lambda item: item["updatedAt"], reverse=True)
        return entries[:limit]

    def operational_health(self) -> dict[str, Any]:
        """Provide one administrator-facing operational health contract."""
        # Open one pooled operational connection and thread it through every
        # helper that reads the control plane. Previously each helper opened
        # its own connection, so a single health check paid five sequential
        # TLS/auth round-trips; the threaded connection pays one.
        with self.operational_connect(read_only=True) as operational_db:
            freshness = self.source_freshness(_operational_db=operational_db)
            cost = self.cost_reconciliation(_operational_db=operational_db)
            latest_sync = self.latest_sync(_operational_db=operational_db)
            operational_queue = operational_db.execute(
                """
                SELECT
                    count(*) FILTER (WHERE status = 'queued'),
                    count(*) FILTER (WHERE status = 'running'),
                    min(started_at) FILTER (
                        WHERE status IN ('queued', 'running')
                    )
                FROM sync_runs
                """
            ).fetchone()
        quality = self.recommendation_quality()
        now = utc_now()
        queue = operational_queue
        oldest_active = queue[2]
        active_age_minutes = (
            round((now - oldest_active).total_seconds() / 60, 1)
            if oldest_active
            else None
        )
        worker_status = (
            "stalled"
            if active_age_minutes is not None and active_age_minutes > 90
            else "busy"
            if int(queue[0] or 0) + int(queue[1] or 0)
            else "ready"
        )
        source_counts = {
            "healthy": sum(1 for item in freshness if item["health"] == "healthy"),
            "stale": sum(1 for item in freshness if item["health"] == "stale"),
            "degraded": sum(
                1 for item in freshness if item["health"] == "degraded"
            ),
        }
        incomplete_cost = sum(
            1
            for dataset in cost["datasets"]
            if not dataset["currentPeriodComplete"] or dataset["failedScopes"]
        )
        overall = (
            "critical"
            if worker_status == "stalled"
            else "degraded"
            if source_counts["degraded"]
            or source_counts["stale"]
            or incomplete_cost
            or quality["status"] == "failed"
            else "healthy"
        )
        return {
            "status": overall,
            "asOf": now.isoformat(),
            "summary": {
                "healthySources": source_counts["healthy"],
                "staleSources": source_counts["stale"],
                "degradedSources": source_counts["degraded"],
                "incompleteCostDatasets": incomplete_cost,
                "queuedRuns": int(queue[0] or 0),
                "runningRuns": int(queue[1] or 0),
            },
            "worker": {
                "status": worker_status,
                "queuedRuns": int(queue[0] or 0),
                "runningRuns": int(queue[1] or 0),
                "oldestActiveAt": (
                    oldest_active.isoformat() if oldest_active else None
                ),
                "activeAgeMinutes": active_age_minutes,
                "latestRun": latest_sync,
            },
            "sources": freshness,
            "costDatasets": cost["datasets"],
            "recommendations": quality,
        }

    def _overview_period_comparison(self) -> dict[str, Any] | None:
        """Month-to-date actual spend vs the same day-span of the prior month.

        Uses finalized billing days only (two-day latency cutoff) so the
        delta compares complete data on both sides. Returns None until both
        windows have any history.
        """
        end = utc_now().date() - timedelta(days=2)
        mtd_start = end.replace(day=1)
        prior_month_end = mtd_start - timedelta(days=1)
        prior_start = prior_month_end.replace(day=1)
        span_days = (end - mtd_start).days
        prior_end = min(
            prior_start + timedelta(days=span_days), prior_month_end
        )
        with self.connect(read_only=True) as db:
            row = db.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN usage_date BETWEEN ? AND ?
                        THEN amount END), 0),
                    COALESCE(SUM(CASE WHEN usage_date BETWEEN ? AND ?
                        THEN amount END), 0),
                    COUNT(CASE WHEN usage_date BETWEEN ? AND ? THEN 1 END),
                    COUNT(CASE WHEN usage_date BETWEEN ? AND ? THEN 1 END)
                FROM daily_cost_history
                WHERE cost_type = 'ActualCost'
                  AND usage_date BETWEEN ? AND ?
                """,
                [
                    mtd_start, end,
                    prior_start, prior_end,
                    mtd_start, end,
                    prior_start, prior_end,
                    prior_start, end,
                ],
            ).fetchone()
        if not row or (not row[2] and not row[3]):
            return None
        current = round(float(row[0]), 2)
        prior = round(float(row[1]), 2)
        delta_percent = (
            round((current - prior) / prior * 100, 1) if prior else None
        )
        return {
            "mtdActual": current,
            "priorMtdActual": prior,
            "deltaPercent": delta_percent,
            "mtdStart": mtd_start.isoformat(),
            "mtdEnd": end.isoformat(),
            "priorStart": prior_start.isoformat(),
            "priorEnd": prior_end.isoformat(),
        }

    def overview(self) -> dict[str, Any]:
        with self.connect(read_only=True) as db:
            summary = db.execute(
                """
                WITH resource_costs AS (
                    SELECT
                        resource_id,
                        SUM(CASE WHEN cost_type = 'ActualCost' THEN amount END) AS actual_cost
                    FROM costs_current
                    WHERE resource_id <> ''
                    GROUP BY resource_id
                ),
                cpu AS (
                    SELECT resource_id, average, source
                    FROM (
                        SELECT resource_id, average, source,
                            row_number() OVER (
                                PARTITION BY resource_id
                                ORDER BY CASE source
                                    WHEN 'azure_monitor' THEN 1 ELSE 2 END,
                                    observed_at DESC
                            ) AS source_rank
                        FROM telemetry_metric_summaries_current
                        WHERE lower(metric) = 'percentage cpu'
                    )
                    WHERE source_rank = 1
                )
                SELECT
                    COUNT(*) AS resource_count,
                    COUNT(DISTINCT resource.subscription_id) AS subscription_count,
                    COUNT(DISTINCT NULLIF(resource.region, '')) AS region_count,
                    SUM(CASE WHEN json_array_length(json_keys(resource.tags_json)) > 0 THEN 1 ELSE 0 END) AS tagged_count,
                    COUNT(COALESCE(cost.actual_cost, resource.estimated_monthly_cost)) AS cost_coverage_count,
                    COALESCE(SUM(COALESCE(cost.actual_cost, resource.estimated_monthly_cost)), 0) AS resource_cost,
                    COUNT(COALESCE(cpu.average, resource.utilization_percent))
                        AS utilization_coverage_count,
                    AVG(COALESCE(cpu.average, resource.utilization_percent))
                        AS average_utilization,
                    SUM(CASE WHEN resource.opportunity_kind IS NOT NULL THEN 1 ELSE 0 END) AS opportunity_count,
                    COALESCE(SUM(resource.estimated_monthly_savings), 0) AS monthly_savings
                FROM resources_current AS resource
                LEFT JOIN resource_costs AS cost
                  ON cost.resource_id = lower(resource.resource_id)
                LEFT JOIN cpu
                  ON cpu.resource_id = lower(resource.resource_id)
                """
            ).fetchone()
            by_type = db.execute(
                """
                SELECT resource_type, COUNT(*) AS value
                FROM resources_current
                GROUP BY resource_type ORDER BY value DESC LIMIT 8
                """
            ).fetchall()
            by_region = db.execute(
                """
                SELECT COALESCE(NULLIF(region, ''), 'global') AS region, COUNT(*) AS value
                FROM resources_current
                GROUP BY region ORDER BY value DESC LIMIT 8
                """
            ).fetchall()
            cost_total = db.execute(
                """
                SELECT SUM(amount)
                FROM costs_current
                WHERE cost_type = 'ActualCost'
                """
            ).fetchone()[0]
            cost_by_type = db.execute(
                """
                SELECT
                    COALESCE(resource.resource_type, 'unallocated') AS resource_type,
                    SUM(cost.amount) AS value
                FROM costs_current AS cost
                LEFT JOIN resources_current AS resource
                  ON lower(resource.resource_id) = cost.resource_id
                WHERE cost.cost_type = 'ActualCost'
                GROUP BY COALESCE(resource.resource_type, 'unallocated')
                ORDER BY value DESC
                LIMIT 8
                """
            ).fetchall()
            if not cost_by_type:
                cost_by_type = db.execute(
                    """
                    SELECT resource_type, SUM(estimated_monthly_cost) AS value
                    FROM resources_current
                    WHERE estimated_monthly_cost IS NOT NULL
                    GROUP BY resource_type ORDER BY value DESC LIMIT 8
                    """
                ).fetchall()
            opportunity_by_kind = db.execute(
                """
                SELECT opportunity_kind, COUNT(*) AS value
                FROM resources_current
                WHERE opportunity_kind IS NOT NULL
                GROUP BY opportunity_kind ORDER BY value DESC
                """
            ).fetchall()
            advisor_summary = db.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(savings_amount), 0)
                FROM advisor_recommendations_current
                WHERE lower(category) IN ('cost', 'performance')
                """
            ).fetchone()
            advisor_by_category = db.execute(
                """
                SELECT
                    'advisor_' || lower(replace(category, ' ', '_')) AS kind,
                    COUNT(*) AS value
                FROM advisor_recommendations_current
                WHERE lower(category) IN ('cost', 'performance')
                GROUP BY category
                ORDER BY value DESC
                """
            ).fetchall()
            intelligence_summary = db.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(estimated_monthly_savings), 0)
                FROM rule_opportunities_current
                """
            ).fetchone()
            governed_valuation = db.execute(
                """
                SELECT
                    count(*) FILTER (WHERE monthly_gross IS NOT NULL),
                    COALESCE(sum(monthly_gross), 0),
                    COALESCE(sum(monthly_risk_adjusted), 0),
                    count(*) FILTER (
                        WHERE value_source LIKE '%_minus_retail_target'
                    )
                FROM (
                    SELECT
                        resource_id,
                        max(monthly_gross) AS monthly_gross,
                        max(monthly_risk_adjusted) AS monthly_risk_adjusted,
                        arg_max(value_source, monthly_gross) AS value_source
                    FROM opportunity_valuation_current
                    GROUP BY resource_id
                )
                """
            ).fetchone()
            intelligence_by_rule = db.execute(
                """
                SELECT 'flux_' || rule_id AS kind, COUNT(*) AS value
                FROM rule_opportunities_current
                GROUP BY rule_id
                ORDER BY value DESC
                """
            ).fetchall()
            utilization_distribution = db.execute(
                """
                WITH cpu AS (
                    SELECT resource_id, average
                    FROM (
                        SELECT resource_id, average,
                            row_number() OVER (
                                PARTITION BY resource_id
                                ORDER BY CASE source
                                    WHEN 'azure_monitor' THEN 1 ELSE 2 END,
                                    observed_at DESC
                            ) AS source_rank
                        FROM telemetry_metric_summaries_current
                        WHERE lower(metric) = 'percentage cpu'
                    )
                    WHERE source_rank = 1
                ),
                utilization AS (
                    SELECT COALESCE(cpu.average, resource.utilization_percent) AS value
                    FROM resources_current AS resource
                    LEFT JOIN cpu
                      ON cpu.resource_id = lower(resource.resource_id)
                    WHERE resource.resource_type = 'microsoft.compute/virtualmachines'
                )
                SELECT
                    CASE
                        WHEN value < 5 THEN 'Idle <5%'
                        WHEN value < 20 THEN 'Low 5–20%'
                        WHEN value < 50 THEN 'Moderate 20–50%'
                        ELSE 'High ≥50%'
                    END AS band,
                    COUNT(*) AS value,
                    CASE
                        WHEN value < 5 THEN 1
                        WHEN value < 20 THEN 2
                        WHEN value < 50 THEN 3
                        ELSE 4
                    END AS band_order
                FROM utilization
                WHERE value IS NOT NULL
                GROUP BY band, band_order
                ORDER BY band_order
                """
            ).fetchall()
            telemetry_coverage = db.execute(
                """
                SELECT
                    CASE
                        WHEN attempt.status = 'covered' THEN 'Covered'
                        WHEN attempt.status = 'no_data' THEN 'No data'
                        WHEN attempt.status = 'error' THEN 'Error'
                        ELSE 'Not attempted'
                    END AS status,
                    COUNT(*) AS value,
                    CASE
                        WHEN attempt.status = 'covered' THEN 1
                        WHEN attempt.status = 'no_data' THEN 2
                        WHEN attempt.status = 'error' THEN 3
                        ELSE 4
                    END AS status_order
                FROM resources_current AS resource
                LEFT JOIN telemetry_resource_attempts_current AS attempt
                  ON attempt.resource_id = lower(resource.resource_id)
                 AND attempt.source = 'azure_monitor'
                WHERE resource.resource_type = 'microsoft.compute/virtualmachines'
                GROUP BY status, status_order
                ORDER BY status_order
                """
            ).fetchall()
            cost_by_subscription = db.execute(
                """
                WITH subscriptions AS (
                    SELECT
                        subscription_id,
                        any_value(NULLIF(subscription_name, '')) AS subscription_name
                    FROM resources_current
                    GROUP BY subscription_id
                )
                SELECT
                    COALESCE(subscription.subscription_name, cost.subscription_id)
                        AS subscription_name,
                    COALESCE(SUM(
                        CASE WHEN cost.cost_type = 'ActualCost' THEN cost.amount END
                    ), 0) AS actual_cost,
                    COALESCE(SUM(
                        CASE WHEN cost.cost_type = 'AmortizedCost' THEN cost.amount END
                    ), 0) AS amortized_cost,
                    cost.subscription_id
                FROM costs_current AS cost
                LEFT JOIN subscriptions AS subscription
                  ON subscription.subscription_id = cost.subscription_id
                GROUP BY cost.subscription_id, subscription.subscription_name
                ORDER BY actual_cost DESC
                LIMIT 8
                """
            ).fetchall()
            commitment_cost_mix = db.execute(
                """
                SELECT
                    CASE lower(replace(pricing_model, ' ', ''))
                        WHEN 'reservation' THEN 'Reservation'
                        WHEN 'savingsplan' THEN 'Savings Plan'
                        WHEN 'ondemand' THEN 'On-demand'
                        WHEN 'spot' THEN 'Spot'
                        ELSE COALESCE(NULLIF(pricing_model, ''), 'Unknown')
                    END AS pricing_model,
                    SUM(greatest(amount, 0)) AS amount
                FROM commitment_costs_current
                GROUP BY pricing_model
                ORDER BY amount DESC
                """
            ).fetchall()
            commitment_summary = db.execute(
                """
                WITH eligibility AS (
                    SELECT
                        meter_id,
                        bool_or(lower(spend_eligibility) = 'eligible')
                            AS spend_eligible,
                        bool_or(lower(usage_eligibility) = 'eligible')
                            AS usage_eligible
                    FROM finops_toolkit_commitment_eligibility
                    GROUP BY meter_id
                ),
                classified AS (
                    SELECT
                        greatest(cost.amount, 0) AS amount,
                        lower(replace(cost.pricing_model, ' ', '')) AS model,
                        eligibility.spend_eligible,
                        eligibility.usage_eligible,
                        eligibility.meter_id IS NOT NULL AS matched,
                        cost.currency,
                        cost.period_start,
                        cost.period_end
                    FROM commitment_costs_current AS cost
                    LEFT JOIN eligibility
                      ON eligibility.meter_id = cost.meter_id
                )
                SELECT
                    count(*) AS row_count,
                    COALESCE(sum(amount) FILTER (
                        WHERE model = 'ondemand'
                          AND (spend_eligible OR usage_eligible)
                    ), 0) AS eligible_on_demand,
                    COALESCE(sum(amount) FILTER (
                        WHERE (model = 'reservation' AND usage_eligible)
                           OR (model = 'savingsplan' AND spend_eligible)
                    ), 0) AS covered,
                    COALESCE(sum(amount) FILTER (WHERE NOT matched), 0)
                        AS unknown_eligibility,
                    count(DISTINCT NULLIF(currency, '')) AS currency_count,
                    any_value(NULLIF(currency, '')) AS currency,
                    min(period_start) AS period_start,
                    max(period_end) AS period_end,
                    count(*) FILTER (WHERE matched) AS matched_rows
                FROM classified
                """
            ).fetchone()
            history = db.execute(
                """
                SELECT CAST(observed_at AS DATE) AS date, COUNT(DISTINCT resource_id) AS value
                FROM resource_snapshots
                GROUP BY CAST(observed_at AS DATE)
                ORDER BY date DESC LIMIT 30
                """
            ).fetchall()
            cost_anomaly_summary = db.execute(
                """
                SELECT
                    count(*) FILTER (WHERE status = 'anomalous'),
                    COALESCE(sum(absolute_change) FILTER (
                        WHERE status = 'anomalous'
                    ), 0),
                    count(DISTINCT NULLIF(currency, '')),
                    any_value(NULLIF(currency, '')),
                    max(evaluation_date)
                FROM cost_anomalies_current
                WHERE cost_type = 'AmortizedCost'
                """
            ).fetchone()
            # NOTE(semantic layer): once every serving snapshot carries the
            # semantic_* views, this and the other ~30 metric derivations in
            # this file should read them instead of re-deriving aggregates.
            # Endpoints must not reference the views before the first
            # post-deploy publication, or reads against an older snapshot
            # fail. The explorer and /api/semantic already read the views
            # with an availability guard.
            daily_cost_trend = db.execute(
                """
                SELECT usage_date, sum(amount)
                FROM daily_cost_history
                WHERE cost_type = 'ActualCost'
                  AND usage_date >= current_date - INTERVAL 30 DAY
                  AND usage_date <= current_date - INTERVAL 2 DAY
                GROUP BY usage_date
                ORDER BY usage_date
                """
            ).fetchall()
        resource_count = summary[0] or 0
        eligible_on_demand = commitment_summary[1] or 0
        covered_commitment = commitment_summary[2] or 0
        eligible_total = eligible_on_demand + covered_commitment
        commitment_status = (
            "not_connected"
            if not commitment_summary[0]
            else "reference_data_unavailable"
            if not commitment_summary[8]
            else "ready"
        )
        with self.operational_connect(read_only=True) as operational_db:
            latest_sync = self.latest_sync(_operational_db=operational_db)
            source_freshness = self.source_freshness(_operational_db=operational_db)
            cost_data_status = self.cost_reconciliation(_operational_db=operational_db)
        period_comparison = self._overview_period_comparison()
        return {
            "summary": {
                "resourceCount": resource_count,
                "subscriptionCount": summary[1] or 0,
                "regionCount": summary[2] or 0,
                "tagCoveragePercent": round((summary[3] or 0) / resource_count * 100, 1)
                if resource_count
                else 0,
                "costCoverageCount": summary[4] or 0,
                "estimatedMonthlyCost": round(
                    cost_total if cost_total is not None else (summary[5] or 0),
                    2,
                ),
                "utilizationCoverageCount": summary[6] or 0,
                "averageUtilizationPercent": round(summary[7], 1)
                if summary[7] is not None
                else None,
                "opportunityCount": governed_valuation[0] or 0,
                "estimatedMonthlySavings": round(
                    governed_valuation[2] or 0,
                    2,
                ),
                "valuedOpportunityCount": governed_valuation[0] or 0,
                "monthlyGrossSavings": round(
                    governed_valuation[1] or 0,
                    2,
                ),
                "monthlyRiskAdjustedSavings": round(
                    governed_valuation[2] or 0,
                    2,
                ),
                "skuValuedOpportunityCount": governed_valuation[3] or 0,
                "costAnomalyCount": cost_anomaly_summary[0] or 0,
                "costAnomalyIncrease": round(
                    cost_anomaly_summary[1] or 0,
                    2,
                ),
                "costAnomalyCurrency": (
                    "Mixed"
                    if (cost_anomaly_summary[2] or 0) > 1
                    else (cost_anomaly_summary[3] or "")
                ),
                "costAnomalyEvaluationDate": (
                    cost_anomaly_summary[4].isoformat()
                    if cost_anomaly_summary[4]
                    else None
                ),
            },
            "periodComparison": period_comparison,
            "resourcesByType": [{"name": row[0], "value": row[1]} for row in by_type],
            "resourcesByRegion": [{"name": row[0], "value": row[1]} for row in by_region],
            "costByType": [{"name": row[0], "value": round(row[1], 2)} for row in cost_by_type],
            "costBySubscription": [
                {
                    "name": row[0],
                    "actual": round(row[1], 2),
                    "amortized": round(row[2], 2),
                    "subscriptionId": row[3],
                }
                for row in cost_by_subscription
            ],
            "commitmentCoverage": {
                "status": commitment_status,
                "eligibleCost": round(eligible_total, 2),
                "coveredCost": round(covered_commitment, 2),
                "eligibleOnDemandCost": round(eligible_on_demand, 2),
                "coveragePercent": round(
                    covered_commitment / eligible_total * 100,
                    1,
                )
                if eligible_total
                else None,
                "unknownEligibilityCost": round(
                    commitment_summary[3] or 0,
                    2,
                ),
                "currency": (
                    commitment_summary[5] or ""
                    if commitment_summary[4] <= 1
                    else "Mixed"
                ),
                "periodStart": (
                    commitment_summary[6].isoformat()
                    if commitment_summary[6]
                    else None
                ),
                "periodEnd": (
                    commitment_summary[7].isoformat()
                    if commitment_summary[7]
                    else None
                ),
                "method": (
                    "Actual month-to-date usage cost grouped by Meter ID and "
                    "pricing model, joined to Microsoft FinOps Toolkit v14 "
                    "commitment eligibility. This is a directional cost mix, "
                    "not benefit utilization."
                ),
            },
            "commitmentCostMix": [
                {"name": row[0], "value": round(row[1], 2)}
                for row in commitment_cost_mix
            ],
            "utilizationDistribution": [
                {"name": row[0], "value": row[1]}
                for row in utilization_distribution
            ],
            "telemetryCoverage": [
                {"name": row[0], "value": row[1]}
                for row in telemetry_coverage
            ],
            "opportunitiesBySource": [
                {"name": "Inventory rules", "value": summary[8] or 0},
                {"name": "Azure Advisor", "value": advisor_summary[0] or 0},
                {"name": "Flux Signals", "value": intelligence_summary[0] or 0},
            ],
            "opportunitiesByKind": [
                {"name": row[0], "value": row[1]}
                for row in [
                    *opportunity_by_kind,
                    *advisor_by_category,
                    *intelligence_by_rule,
                ]
            ],
            "inventoryHistory": [
                {"date": row[0].isoformat(), "value": row[1]} for row in reversed(history)
            ],
            "dailyCostTrend": [
                {"date": row[0].isoformat(), "amount": round(row[1] or 0, 2)}
                for row in daily_cost_trend
            ],
            "latestSync": latest_sync,
            "sourceFreshness": source_freshness,
            "costDataStatus": cost_data_status,
        }

    def inventory(
        self,
        *,
        search: str = "",
        resource_type: str = "",
        subscription_id: str = "",
        region: str = "",
        virtual_tag_key: str = "",
        virtual_tag_value: str = "",
        opportunity_only: bool = False,
        limit: int = 250,
        offset: int = 0,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        params: list[Any] = []
        if search:
            conditions.append(
                "(resource.name ILIKE ? OR resource.resource_group ILIKE ? "
                "OR resource.resource_type ILIKE ? OR resource.resource_id ILIKE ?)"
            )
            token = f"%{search}%"
            params.extend([token, token, token, token])
        if resource_type:
            conditions.append("resource.resource_type = ?")
            params.append(resource_type)
        if subscription_id:
            conditions.append("resource.subscription_id = ?")
            params.append(subscription_id)
        if region:
            conditions.append("resource.region = ?")
            params.append(region)
        if opportunity_only:
            conditions.append("resource.opportunity_kind IS NOT NULL")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect(read_only=True) as db:
            total = db.execute(
                f"SELECT COUNT(*) FROM resources_current AS resource {where}", params
            ).fetchone()[0]
            query_limit = 1000000 if virtual_tag_key else limit
            query_offset = 0 if virtual_tag_key else offset
            rows = db.execute(
                f"""
                WITH resource_costs AS (
                    SELECT
                        resource_id,
                        SUM(CASE WHEN cost_type = 'ActualCost' THEN amount END) AS actual_cost,
                        SUM(CASE WHEN cost_type = 'AmortizedCost' THEN amount END) AS amortized_cost,
                        max(currency) AS currency
                    FROM costs_current
                    WHERE resource_id <> ''
                    GROUP BY resource_id
                ),
                cpu AS (
                    SELECT resource_id, average, source
                    FROM (
                        SELECT resource_id, average, source,
                            row_number() OVER (
                                PARTITION BY resource_id
                                ORDER BY CASE source
                                    WHEN 'azure_monitor' THEN 1 ELSE 2 END,
                                    observed_at DESC
                            ) AS source_rank
                        FROM telemetry_metric_summaries_current
                        WHERE lower(metric) = 'percentage cpu'
                    )
                    WHERE source_rank = 1
                )
                SELECT
                       resource.resource_id, resource.name, resource.resource_type,
                       resource.subscription_id, resource.subscription_name,
                       resource.resource_group, resource.region, resource.kind,
                       resource.sku, resource.provisioning_state, resource.managed_by,
                       resource.tags_json,
                       COALESCE(cost.actual_cost, resource.estimated_monthly_cost) AS actual_cost,
                       CASE WHEN cost.actual_cost IS NOT NULL
                            THEN 'azure_cost_management_query'
                            ELSE resource.cost_source END AS cost_source,
                       COALESCE(cpu.average, resource.utilization_percent),
                       COALESCE(cpu.source, resource.utilization_source),
                       resource.opportunity_kind, resource.opportunity_reason,
                       resource.estimated_monthly_savings, resource.observed_at,
                       cost.amortized_cost, cost.currency
                FROM resources_current AS resource
                LEFT JOIN resource_costs AS cost
                  ON cost.resource_id = lower(resource.resource_id)
                LEFT JOIN cpu ON cpu.resource_id = lower(resource.resource_id)
                {where}
                ORDER BY resource.name, resource.resource_type
                LIMIT ? OFFSET ?
                """,
                [*params, query_limit, query_offset],
            ).fetchall()
            facets = {
                "resourceTypes": [
                    row[0]
                    for row in db.execute(
                        "SELECT DISTINCT resource_type FROM resources_current ORDER BY 1"
                    ).fetchall()
                ],
                "subscriptions": [
                    {"id": row[0], "name": row[1] or row[0]}
                    for row in db.execute(
                        """
                        SELECT DISTINCT subscription_id, subscription_name
                        FROM resources_current ORDER BY 2, 1
                        """
                    ).fetchall()
                ],
                "regions": [
                    row[0]
                    for row in db.execute(
                        "SELECT DISTINCT region FROM resources_current WHERE region <> '' ORDER BY 1"
                    ).fetchall()
                ],
            }
        labels = self.subscription_labels()
        items = [
                {
                    "resourceId": row[0],
                    "name": row[1],
                    "resourceType": row[2],
                    "subscriptionId": row[3],
                    "subscriptionName": (
                        row[4]
                        if row[4] and row[4] != row[3]
                        else labels.get(str(row[3] or "").lower())
                        or row[4]
                        or row[3]
                    ),
                    "resourceGroup": row[5],
                    "region": row[6],
                    "kind": row[7],
                    "sku": row[8],
                    "provisioningState": row[9],
                    "managedBy": row[10],
                    "tags": json.loads(row[11] or "{}"),
                    "estimatedMonthlyCost": row[12],
                    "costSource": row[13],
                    "utilizationPercent": row[14],
                    "utilizationSource": row[15],
                    "opportunityKind": row[16],
                    "opportunityReason": row[17],
                    "estimatedMonthlySavings": row[18],
                    "observedAt": row[19].isoformat(),
                    "amortizedMonthlyCost": row[20],
                    "costCurrency": row[21] or "",
                }
                for row in rows
            ]
        if items:
            from .virtual_tags import effective_tags
            rules = self.virtual_tag_rules(include_inactive=False)
            resource_ids = [str(item["resourceId"]).lower() for item in items]
            overrides = self.virtual_tag_overrides_for(resource_ids)
            for item in items:
                normalized = str(item["resourceId"]).lower()
                item["effectiveVirtualTags"] = effective_tags(
                    {
                        **item,
                        "tags": item["tags"],
                    },
                    rules, overrides.get(normalized, []), utc_now().date(),
                )
        if virtual_tag_key:
            def selected_value(item: dict[str, Any]) -> str:
                match = next(
                    (tag for key, tag in item["effectiveVirtualTags"].items()
                     if key.lower() == virtual_tag_key.lower()),
                    None,
                )
                return str(match.get("value") or "") if match else ""
            items = [
                item for item in items
                if selected_value(item)
                and (not virtual_tag_value or selected_value(item).lower() == virtual_tag_value.lower())
            ]
            total = len(items)
            items = items[offset:offset + limit]
        facets["virtualTagDimensions"] = self.virtual_tag_dimensions(include_inactive=False)
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "facets": facets,
        }

    def start_telemetry_run(self, source: str, trigger: str = "scheduled") -> str:
        run_id = str(uuid4())
        with self.connect() as db:
            db.execute(
                "INSERT INTO telemetry_runs VALUES (?, ?, ?, ?, NULL, 'running', 0, '')",
                [run_id, source, trigger, utc_now()],
            )
        return run_id

    def finish_telemetry_run(
        self,
        run_id: str,
        status: str,
        processed_count: int,
        message: str,
        completed_at: datetime | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE telemetry_runs
                SET completed_at = ?, status = ?, processed_count = ?, message = ?
                WHERE id = ?
                """,
                [completed_at or utc_now(), status, processed_count, message, run_id],
            )

    def start_telemetry_import(
        self,
        run_id: str,
        source: str,
        started_at: datetime,
    ) -> None:
        """Start or idempotently replace one governed bootstrap import."""
        with self.connect() as db:
            db.execute("BEGIN TRANSACTION")
            try:
                db.execute(
                    "DELETE FROM telemetry_metric_summaries WHERE run_id = ?",
                    [run_id],
                )
                db.execute(
                    "DELETE FROM telemetry_resource_attempts WHERE run_id = ?",
                    [run_id],
                )
                db.execute(
                    "DELETE FROM resource_source_matches WHERE run_id = ?",
                    [run_id],
                )
                db.execute("DELETE FROM telemetry_runs WHERE id = ?", [run_id])
                db.execute(
                    """
                    INSERT INTO telemetry_runs VALUES (
                        ?, ?, 'bootstrap_import', ?, NULL, 'running', 0, ''
                    )
                    """,
                    [run_id, source, started_at],
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def telemetry_targets(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect(read_only=True) as db:
            rows = db.execute(
                """
                SELECT resource.resource_id, resource.name, resource.subscription_id,
                       resource.subscription_name, resource.resource_group,
                       resource.region, resource.raw_json,
                       attempt.observed_at AS last_attempt_at
                FROM resources_current AS resource
                LEFT JOIN telemetry_resource_attempts_current AS attempt
                  ON attempt.resource_id = lower(resource.resource_id)
                 AND attempt.source = 'azure_monitor'
                WHERE resource.resource_type = 'microsoft.compute/virtualmachines'
                ORDER BY last_attempt_at NULLS FIRST, resource.name
                LIMIT ?
                """,
                [limit],
            ).fetchall()
        return [
            {
                "resourceId": row[0],
                "name": row[1],
                "subscriptionId": row[2],
                "subscriptionName": row[3],
                "resourceGroup": row[4],
                "region": row[5],
                "raw": json.loads(row[6] or "{}"),
            }
            for row in rows
        ]

    def logicmonitor_metric_targets(
        self,
        limit: int,
        *,
        initial_hours: int,
        maximum_window_hours: int,
    ) -> list[dict[str, Any]]:
        with self.connect(read_only=True) as db:
            rows = db.execute(
                """
                SELECT match.source_resource_id, match.source_name,
                       match.resource_id,
                       coalesce(
                           json_extract_string(match.details_json, '$.platform'),
                           'Unknown'
                       ) AS platform,
                       checkpoint.collected_through
                FROM resource_source_matches_current AS match
                LEFT JOIN telemetry_collection_checkpoints AS checkpoint
                  ON checkpoint.source = 'logicmonitor'
                 AND checkpoint.source_resource_id = match.source_resource_id
                 AND checkpoint.stream = 'performance'
                JOIN resources_current AS resource
                  ON lower(resource.resource_id) = match.resource_id
                WHERE match.source = 'logicmonitor'
                  AND match.status = 'matched'
                  AND resource.resource_type =
                      'microsoft.compute/virtualmachines'
                ORDER BY checkpoint.collected_through NULLS FIRST,
                         match.source_name
                LIMIT ?
                """,
                [limit],
            ).fetchall()
        now = utc_now().replace(microsecond=0)
        values = []
        for row in rows:
            checkpoint = row[4]
            start = (
                checkpoint - timedelta(minutes=5)
                if checkpoint
                else now - timedelta(hours=initial_hours)
            )
            end = min(
                now,
                (checkpoint or start)
                + timedelta(hours=maximum_window_hours),
            )
            values.append(
                {
                    "sourceResourceId": row[0],
                    "sourceName": row[1],
                    "resourceId": row[2],
                    "platform": row[3],
                    "windowStart": start,
                    "windowEnd": end,
                }
            )
        return values

    def store_telemetry_samples(
        self,
        run_id: str,
        samples: list[dict[str, Any]],
        *,
        retention_days: int = 30,
    ) -> None:
        """Upsert one target's samples.

        Deliberately does NOT prune here: this runs once per metric target,
        and the retention DELETE it used to carry was a full scan of the
        multi-million-row samples table inside the same transaction --
        dozens of times per run. Under the DuckDB memory cap that is what
        drove the LogicMonitor metrics job out of memory ("failed to pin
        block ... 1.4 GiB/1.4 GiB used", every run from mid-July to
        2026-08-02, leaving the source permanently degraded). Callers prune
        once per run via prune_telemetry_samples.
        """
        if not samples:
            return
        ingested_at = utc_now()
        window_start = min(item["observedAt"] for item in samples)
        window_end = max(item["observedAt"] for item in samples)
        batch_scopes = sorted(
            {
                (item["source"], str(item["sourceResourceId"]))
                for item in samples
            }
        )
        with self.connect() as db:
            # Insert-heavy path against a large table under a hard memory
            # cap; insertion order carries no meaning for samples and
            # preserving it blocks spilling.
            db.execute("SET preserve_insertion_order = false")
            db.execute("BEGIN TRANSACTION")
            try:
                # Explicit idempotency instead of a PK upsert (see the
                # schema comment): re-collections replace their own
                # (source, resource, window) slice via a spillable scan.
                for source, source_resource_id in batch_scopes:
                    db.execute(
                        """
                        DELETE FROM telemetry_metric_samples
                        WHERE source = ? AND source_resource_id = ?
                          AND observed_at BETWEEN ? AND ?
                        """,
                        [source, source_resource_id, window_start, window_end],
                    )
                db.executemany(
                    """
                    INSERT INTO telemetry_metric_samples (
                        run_id, ingested_at, source, source_resource_id,
                        resource_id, metric, unit, observed_at, value,
                        lineage_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        [
                            run_id,
                            ingested_at,
                            item["source"],
                            str(item["sourceResourceId"]),
                            item["resourceId"].lower(),
                            item["metric"],
                            item.get("unit", ""),
                            item["observedAt"],
                            item["value"],
                            json_value(item.get("lineage", {})),
                        ]
                        for item in samples
                    ],
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def prune_telemetry_samples(self, retention_days: int = 30) -> int:
        """One retention pass over raw samples; call once per import run."""
        with self.connect() as db:
            return int(
                db.execute(
                    """
                    DELETE FROM telemetry_metric_samples
                    WHERE observed_at < now() - (? * INTERVAL '1 day')
                    """,
                    [max(1, retention_days)],
                ).fetchone()[0]
            )

    def update_telemetry_checkpoint(
        self,
        source_resource_id: str,
        collected_through: datetime,
        *,
        status: str,
        message: str,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO telemetry_collection_checkpoints VALUES (
                    'logicmonitor', ?, 'performance', ?, ?, ?, ?
                )
                ON CONFLICT (source, source_resource_id, stream)
                DO UPDATE SET
                    collected_through = excluded.collected_through,
                    updated_at = excluded.updated_at,
                    status = excluded.status,
                    message = excluded.message
                """,
                [
                    str(source_resource_id),
                    collected_through,
                    utc_now(),
                    status,
                    message[:500],
                ],
            )

    def summarize_logicmonitor_samples(
        self,
        run_id: str,
        resource_ids: list[str],
        *,
        history_days: int,
    ) -> int:
        if not resource_ids:
            return 0
        normalized = sorted({item.lower() for item in resource_ids})
        placeholders = ", ".join("?" for _ in normalized)
        cutoff = utc_now() - timedelta(days=history_days)
        with self.connect(read_only=True) as db:
            rows = db.execute(
                f"""
                SELECT resource_id, metric, any_value(unit),
                       min(observed_at), max(observed_at), count(*),
                       count(DISTINCT date_trunc('hour', observed_at)),
                       avg(value), quantile_cont(value, 0.95), max(value),
                       arg_max(value, observed_at),
                       arg_max(observed_at, observed_at),
                       any_value(source_resource_id)
                FROM telemetry_metric_samples
                WHERE source = 'logicmonitor'
                  AND observed_at >= ?
                  AND resource_id IN ({placeholders})
                GROUP BY resource_id, metric
                """,
                [cutoff, *normalized],
            ).fetchall()
        summaries = [
            {
                "resourceId": row[0],
                "source": "logicmonitor",
                "metric": row[1],
                "unit": row[2],
                "windowStart": row[3],
                "windowEnd": row[4],
                "sampleCount": row[5],
                "coveragePercent": min(
                    100.0,
                    row[6] / max(1, history_days * 24) * 100,
                ),
                "average": row[7],
                "p95": row[8],
                "maximum": row[9],
                "lastValue": row[10],
                "lastObservedAt": row[11],
                "aggregationMethod": (
                    f"Rolling {history_days}-day LogicMonitor samples; "
                    "hour-bucket coverage."
                ),
                "lineage": {
                    "sourceSystem": "LogicMonitor",
                    "deviceId": row[12],
                    "method": "checkpointed_incremental_v1",
                    "coverageSemantics": (
                        "Distinct observed hourly buckets divided by the "
                        "governed history window."
                    ),
                },
            }
            for row in rows
        ]
        self.store_telemetry_summaries(run_id, summaries)
        return len(summaries)

    def store_telemetry_summaries(
        self,
        run_id: str,
        summaries: list[dict[str, Any]],
        *,
        observed_at: datetime | None = None,
    ) -> None:
        if not summaries:
            return
        observed_at = observed_at or utc_now()
        rows = [
            [
                run_id,
                observed_at,
                item["resourceId"].lower(),
                item["source"],
                item["metric"],
                item.get("unit", ""),
                item["windowStart"],
                item["windowEnd"],
                item["sampleCount"],
                item["coveragePercent"],
                item.get("average"),
                item.get("p95"),
                item.get("maximum"),
                item.get("lastValue"),
                item.get("lastObservedAt"),
                item.get("aggregationMethod", ""),
                json_value(item.get("lineage", {})),
            ]
            for item in summaries
        ]
        with self.connect() as db:
            db.executemany(
                """
                INSERT INTO telemetry_metric_summaries (
                    run_id, observed_at, resource_id, source, metric, unit,
                    window_start, window_end, sample_count, coverage_percent,
                    average, p95, maximum, last_value, last_observed_at,
                    aggregation_method, lineage_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def store_telemetry_attempts(
        self,
        run_id: str,
        attempts: list[dict[str, Any]],
        *,
        observed_at: datetime | None = None,
    ) -> None:
        if not attempts:
            return
        observed_at = observed_at or utc_now()
        with self.connect() as db:
            db.executemany(
                "INSERT INTO telemetry_resource_attempts VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    [
                        run_id,
                        observed_at,
                        item["resourceId"].lower(),
                        item["source"],
                        item["status"],
                        item.get("metricCount", 0),
                        str(item.get("message", ""))[:500],
                    ]
                    for item in attempts
                ],
            )

    def store_source_matches(
        self,
        run_id: str,
        matches: list[dict[str, Any]],
        *,
        observed_at: datetime | None = None,
    ) -> None:
        if not matches:
            return
        observed_at = observed_at or utc_now()
        with self.connect() as db:
            db.executemany(
                "INSERT INTO resource_source_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    [
                        run_id,
                        observed_at,
                        item["source"],
                        item["sourceResourceId"],
                        item["sourceName"],
                        item["resourceId"].lower(),
                        item["status"],
                        item["method"],
                        item["confidence"],
                        json_value(item.get("details")),
                    ]
                    for item in matches
                ],
            )

    def replace_finops_toolkit_open_data(
        self,
        files: dict[str, Path],
        datasets: dict[str, dict[str, str]],
    ) -> dict[str, int]:
        """Load checksum-verified Microsoft FinOps Toolkit datasets."""
        imported_at = utc_now()
        inserts = {
            "ResourceTypes": """
                INSERT INTO finops_toolkit_resource_types
                SELECT ?, ?, lower(coalesce("ResourceType", '')),
                    coalesce("SingularDisplayName", ''),
                    coalesce("PluralDisplayName", ''),
                    coalesce("LowerSingularDisplayName", ''),
                    coalesce("LowerPluralDisplayName", ''),
                    try_cast("IsPreview" AS BOOLEAN),
                    coalesce("Description", ''), coalesce("Icon", ''),
                    CASE WHEN trim(coalesce("Links", '')) = '' THEN '[]'
                        ELSE "Links" END::JSON
                FROM _finops_toolkit_source
            """,
            "Regions": """
                INSERT INTO finops_toolkit_regions
                SELECT ?, ?, lower(coalesce("OriginalValue", '')),
                    lower(coalesce("RegionId", '')),
                    coalesce("RegionName", '')
                FROM _finops_toolkit_source
            """,
            "Services": """
                INSERT INTO finops_toolkit_services
                SELECT ?, ?, lower(coalesce("ConsumedService", '')),
                    lower(coalesce("ResourceType", '')),
                    coalesce("ServiceName", ''),
                    coalesce("ServiceCategory", ''),
                    coalesce("ServiceSubcategory", ''),
                    coalesce("PublisherName", ''),
                    coalesce("PublisherType", ''),
                    coalesce("Environment", ''),
                    coalesce("ServiceModel", '')
                FROM _finops_toolkit_source
            """,
            "PricingUnits": """
                INSERT INTO finops_toolkit_pricing_units
                SELECT ?, ?, coalesce("UnitOfMeasure", ''),
                    coalesce("AccountTypes", ''),
                    try_cast("PricingBlockSize" AS DOUBLE),
                    coalesce("DistinctUnits", '')
                FROM _finops_toolkit_source
            """,
            "CommitmentDiscountEligibility": """
                INSERT INTO finops_toolkit_commitment_eligibility
                SELECT ?, ?, lower(coalesce("MeterId", '')),
                    coalesce("x_CommitmentDiscountSpendEligibility", ''),
                    coalesce("x_CommitmentDiscountUsageEligibility", '')
                FROM _finops_toolkit_source
            """,
        }
        target_tables = {
            "ResourceTypes": "finops_toolkit_resource_types",
            "Regions": "finops_toolkit_regions",
            "Services": "finops_toolkit_services",
            "PricingUnits": "finops_toolkit_pricing_units",
            "CommitmentDiscountEligibility": (
                "finops_toolkit_commitment_eligibility"
            ),
        }
        counts: dict[str, int] = {}
        with self.connect() as db:
            db.execute("BEGIN TRANSACTION")
            try:
                for dataset, path in files.items():
                    metadata = datasets[dataset]
                    version = metadata["toolkitVersion"]
                    relation = db.read_csv(
                        str(path),
                        header=True,
                        all_varchar=True,
                    )
                    relation.create_view(
                        "_finops_toolkit_source",
                        replace=True,
                    )
                    row_count = db.execute(
                        "SELECT count(*) FROM _finops_toolkit_source"
                    ).fetchone()[0]
                    db.execute(
                        f"DELETE FROM {target_tables[dataset]} "
                        "WHERE toolkit_version = ?",
                        [version],
                    )
                    db.execute(inserts[dataset], [version, imported_at])
                    db.execute(
                        """
                        DELETE FROM finops_toolkit_dataset_versions
                        WHERE dataset = ? AND toolkit_version = ?
                        """,
                        [dataset, version],
                    )
                    db.execute(
                        """
                        INSERT INTO finops_toolkit_dataset_versions VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        [
                            dataset,
                            version,
                            metadata["upstreamCommit"],
                            metadata["sourceUrl"],
                            metadata["sha256"],
                            imported_at,
                            row_count,
                            metadata["license"],
                        ],
                    )
                    counts[dataset] = row_count
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return counts

    def finops_toolkit_status(self) -> dict[str, Any]:
        with self.connect(read_only=True) as db:
            rows = db.execute(
                """
                SELECT dataset, toolkit_version, upstream_commit, source_url,
                       sha256, imported_at, row_count, license
                FROM finops_toolkit_dataset_versions
                QUALIFY row_number() OVER (
                    PARTITION BY dataset ORDER BY imported_at DESC
                ) = 1
                ORDER BY dataset
                """
            ).fetchall()
        return {
            "datasets": [
                {
                    "dataset": row[0],
                    "toolkitVersion": row[1],
                    "upstreamCommit": row[2],
                    "sourceUrl": row[3],
                    "sha256": row[4],
                    "importedAt": row[5].isoformat(),
                    "rowCount": row[6],
                    "license": row[7],
                }
                for row in rows
            ]
        }

    def telemetry_status(self) -> dict[str, Any]:
        with self.connect(read_only=True) as db:
            runs = db.execute(
                """
                SELECT source, id, trigger, started_at, completed_at, status,
                       processed_count, message
                FROM (
                    SELECT *, row_number() OVER (
                        PARTITION BY source ORDER BY started_at DESC
                    ) AS rank
                    FROM telemetry_runs
                ) WHERE rank = 1 ORDER BY source
                """
            ).fetchall()
            vm_count = db.execute(
                "SELECT count(*) FROM resources_current WHERE resource_type = 'microsoft.compute/virtualmachines'"
            ).fetchone()[0]
            azure_count = db.execute(
                "SELECT count(DISTINCT resource_id) FROM telemetry_metric_summaries_current WHERE source = 'azure_monitor'"
            ).fetchone()[0]
            logicmonitor_metric_count = db.execute(
                """
                SELECT count(DISTINCT resource_id)
                FROM telemetry_metric_summaries_current
                WHERE source = 'logicmonitor'
                """
            ).fetchone()[0]
            checkpoint_status = db.execute(
                """
                SELECT count(*), min(collected_through),
                       max(collected_through)
                FROM telemetry_collection_checkpoints
                WHERE source = 'logicmonitor'
                  AND stream = 'performance'
                """
            ).fetchone()
            attempt_counts = dict(
                db.execute(
                    """
                    SELECT status, count(*) FROM telemetry_resource_attempts_current
                    WHERE source = 'azure_monitor' GROUP BY status
                    """
                ).fetchall()
            )
            match_counts = dict(
                db.execute(
                    """
                    SELECT status, count(*) FROM resource_source_matches_current
                    WHERE source = 'logicmonitor' GROUP BY status
                    """
                ).fetchall()
            )
            subscription_rows = db.execute(
                """
                WITH vms AS (
                    SELECT
                        lower(resource_id) AS resource_id,
                        subscription_id,
                        any_value(NULLIF(subscription_name, ''))
                            AS subscription_name
                    FROM resources_current
                    WHERE resource_type =
                        'microsoft.compute/virtualmachines'
                    GROUP BY resource_id, subscription_id
                ),
                azure_metrics AS (
                    SELECT DISTINCT resource_id
                    FROM telemetry_metric_summaries_current
                    WHERE source = 'azure_monitor'
                ),
                logicmonitor_metrics AS (
                    SELECT DISTINCT resource_id
                    FROM telemetry_metric_summaries_current
                    WHERE source = 'logicmonitor'
                ),
                logicmonitor_matches AS (
                    SELECT DISTINCT resource_id
                    FROM resource_source_matches_current
                    WHERE source = 'logicmonitor' AND status = 'matched'
                )
                SELECT
                    vm.subscription_id,
                    COALESCE(
                        any_value(vm.subscription_name),
                        vm.subscription_id
                    ) AS subscription_name,
                    count(*) AS virtual_machines,
                    count(attempt.resource_id) AS azure_attempted,
                    count(azure_metrics.resource_id) AS azure_covered,
                    count(*) FILTER (WHERE attempt.status = 'no_data')
                        AS azure_no_data,
                    count(*) FILTER (WHERE attempt.status = 'error')
                        AS azure_errors,
                    count(logicmonitor_matches.resource_id) AS lm_matched,
                    count(logicmonitor_metrics.resource_id) AS lm_covered,
                    count(*) FILTER (
                        WHERE rightsizing.status = 'candidate'
                    ) AS candidates,
                    count(*) FILTER (
                        WHERE rightsizing.status = 'warming_up'
                    ) AS warming_up,
                    count(*) FILTER (
                        WHERE rightsizing.status IN (
                            'insufficient_telemetry', 'partial_telemetry'
                        )
                    ) AS insufficient
                FROM vms AS vm
                LEFT JOIN telemetry_resource_attempts_current AS attempt
                  ON attempt.resource_id = vm.resource_id
                 AND attempt.source = 'azure_monitor'
                LEFT JOIN azure_metrics
                  ON azure_metrics.resource_id = vm.resource_id
                LEFT JOIN logicmonitor_matches
                  ON logicmonitor_matches.resource_id = vm.resource_id
                LEFT JOIN logicmonitor_metrics
                  ON logicmonitor_metrics.resource_id = vm.resource_id
                LEFT JOIN rightsizing_recommendations_current AS rightsizing
                  ON rightsizing.resource_id = vm.resource_id
                GROUP BY vm.subscription_id
                ORDER BY virtual_machines DESC, subscription_name
                """
            ).fetchall()
        return {
            "virtualMachineCount": vm_count,
            "azureMonitorCovered": azure_count,
            "azureMonitorAttempted": sum(attempt_counts.values()),
            "azureMonitorNoData": attempt_counts.get("no_data", 0),
            "azureMonitorErrors": attempt_counts.get("error", 0),
            "logicMonitorMatched": match_counts.get("matched", 0),
            "logicMonitorAmbiguous": match_counts.get("ambiguous", 0),
            "logicMonitorUnmatched": match_counts.get("unmatched", 0),
            "logicMonitorMetricCovered": logicmonitor_metric_count,
            "logicMonitorCheckpointed": checkpoint_status[0] or 0,
            "logicMonitorOldestCheckpoint": (
                checkpoint_status[1].isoformat()
                if checkpoint_status[1]
                else None
            ),
            "logicMonitorNewestCheckpoint": (
                checkpoint_status[2].isoformat()
                if checkpoint_status[2]
                else None
            ),
            "bySubscription": [
                {
                    "subscriptionId": row[0],
                    "subscriptionName": row[1],
                    "virtualMachines": row[2],
                    "azureMonitorAttempted": row[3],
                    "azureMonitorCovered": row[4],
                    "azureMonitorNoData": row[5],
                    "azureMonitorErrors": row[6],
                    "logicMonitorMatched": row[7],
                    "logicMonitorCovered": row[8],
                    "candidates": row[9],
                    "warmingUp": row[10],
                    "insufficient": row[11],
                }
                for row in subscription_rows
            ],
            "runs": [
                {
                    "source": row[0],
                    "id": row[1],
                    "trigger": row[2],
                    "startedAt": row[3].isoformat(),
                    "completedAt": row[4].isoformat() if row[4] else None,
                    "status": row[5],
                    "processedCount": row[6],
                    "message": row[7],
                }
                for row in runs
            ],
        }

    def fleet_telemetry(
        self,
        *,
        subscription_id: str = "",
        resource_type: str = "microsoft.compute/virtualmachines",
        region: str = "",
        search: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        """Utilization summaries for many resources in one governed read.

        The per-resource ``resource_telemetry`` path costs one call per VM,
        which exhausts an assistant's tool budget on any fleet-scale
        question. This returns the same governed metric summaries pivoted to
        one row per resource, with cost and coverage attached, so fleet
        analysis fits in a single call.
        """
        limit = max(1, min(int(limit), 500))
        conditions = ["1 = 1"]
        params: list[Any] = []
        if subscription_id:
            conditions.append("resource.subscription_id = ?")
            params.append(subscription_id.lower())
        if resource_type:
            conditions.append("lower(resource.resource_type) = ?")
            params.append(resource_type.lower())
        if region:
            conditions.append("lower(resource.region) = ?")
            params.append(region.lower())
        if search:
            conditions.append(
                "(resource.name ILIKE ? OR resource.resource_group ILIKE ?)"
            )
            token = f"%{search}%"
            params.extend([token, token])
        where = " AND ".join(conditions)
        with self.connect(read_only=True) as db:
            rows = db.execute(
                f"""
                WITH resource_costs AS (
                    SELECT lower(resource_id) AS resource_id,
                           SUM(CASE WHEN cost_type = 'ActualCost'
                               THEN amount END) AS actual_cost,
                           any_value(currency) AS currency
                    FROM costs_current
                    GROUP BY lower(resource_id)
                ),
                metrics AS (
                    -- Metric names match the canonical governed names used by
                    -- compute_rightsizing_recommendations, so fleet analysis
                    -- and right-sizing read identical evidence.
                    SELECT
                        resource_id,
                        MAX(CASE WHEN lower(metric) = 'percentage cpu'
                            THEN average END) AS cpu_avg,
                        MAX(CASE WHEN lower(metric) = 'percentage cpu'
                            THEN p95 END) AS cpu_p95,
                        MAX(CASE WHEN lower(metric) = 'percentage cpu'
                            THEN maximum END) AS cpu_max,
                        MAX(CASE WHEN lower(metric) = 'memory used percentage'
                            THEN average END) AS memory_avg,
                        MAX(CASE WHEN lower(metric) = 'memory used percentage'
                            THEN p95 END) AS memory_p95,
                        MAX(CASE WHEN lower(metric) = 'network in total'
                            THEN p95 END) AS network_in_p95,
                        MAX(CASE WHEN lower(metric) = 'network out total'
                            THEN p95 END) AS network_out_p95,
                        MAX(coverage_percent) AS coverage_percent,
                        MAX(sample_count) AS sample_count,
                        MIN(window_start) AS window_start,
                        MAX(window_end) AS window_end,
                        string_agg(DISTINCT source, ',') AS sources
                    FROM telemetry_metric_summaries_current
                    GROUP BY resource_id
                )
                SELECT
                    resource.resource_id,
                    resource.name,
                    resource.resource_type,
                    resource.sku,
                    resource.region,
                    resource.subscription_id,
                    resource.subscription_name,
                    resource.resource_group,
                    metrics.cpu_avg, metrics.cpu_p95, metrics.cpu_max,
                    metrics.memory_avg, metrics.memory_p95,
                    metrics.network_in_p95, metrics.network_out_p95,
                    metrics.coverage_percent, metrics.sample_count,
                    metrics.window_start, metrics.window_end, metrics.sources,
                    cost.actual_cost, cost.currency
                FROM resources_current AS resource
                LEFT JOIN metrics
                  ON metrics.resource_id = lower(resource.resource_id)
                LEFT JOIN resource_costs AS cost
                  ON cost.resource_id = lower(resource.resource_id)
                WHERE {where}
                ORDER BY cost.actual_cost DESC NULLS LAST, resource.name
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
            total = db.execute(
                f"""
                SELECT count(*) FROM resources_current AS resource
                WHERE {where}
                """,
                params,
            ).fetchone()[0]
        def number(value: Any, digits: int = 1) -> float | None:
            return round(float(value), digits) if value is not None else None

        items = []
        covered = 0
        for row in rows:
            # CPU p95 is the metric right-sizing decisions rest on; a resource
            # with a coverage row but no CPU series is not usable evidence.
            has_cpu = row[9] is not None
            if has_cpu:
                covered += 1
            items.append(
                {
                    "resourceId": row[0],
                    "name": row[1],
                    "resourceType": row[2],
                    "sku": row[3] or "",
                    "region": row[4] or "",
                    "subscriptionId": row[5],
                    "subscriptionName": row[6] or "",
                    "resourceGroup": row[7] or "",
                    "cpuAverage": number(row[8]),
                    "cpuP95": number(row[9]),
                    "cpuMaximum": number(row[10]),
                    "memoryAverage": number(row[11]),
                    "memoryP95": number(row[12]),
                    "networkInP95Bytes": number(row[13]),
                    "networkOutP95Bytes": number(row[14]),
                    "coveragePercent": number(row[15]),
                    "sampleCount": int(row[16]) if row[16] is not None else 0,
                    "windowStart": row[17].isoformat() if row[17] else None,
                    "windowEnd": row[18].isoformat() if row[18] else None,
                    "telemetrySources": (
                        sorted(str(row[19]).split(",")) if row[19] else []
                    ),
                    "actualMonthlyCost": number(row[20], 2),
                    "costCurrency": row[21] or "",
                    "telemetryStatus": (
                        "covered" if has_cpu else "no_cpu_evidence"
                    ),
                }
            )
        return {
            "items": items,
            "returned": len(items),
            "matching": total,
            "truncated": total > len(items),
            "cpuEvidenceCount": covered,
            "filters": {
                "subscriptionId": subscription_id,
                "resourceType": resource_type,
                "region": region,
                "search": search,
                "limit": limit,
            },
            "limitations": (
                []
                if total <= len(items)
                else [
                    f"{total:,} resources match; the {len(items):,} highest-cost "
                    "are returned. Narrow by subscription or region for the rest."
                ]
            ),
        }

    def resource_telemetry(self, resource_id: str) -> dict[str, Any]:
        normalized = resource_id.lower()
        with self.connect(read_only=True) as db:
            metrics = db.execute(
                """
                SELECT source, metric, unit, window_start, window_end,
                       sample_count, coverage_percent, average, p95, maximum,
                       last_value, last_observed_at, aggregation_method,
                       lineage_json
                FROM telemetry_metric_summaries_current
                WHERE resource_id = ?
                ORDER BY source, metric
                """,
                [normalized],
            ).fetchall()
            matches = db.execute(
                """
                SELECT source, source_resource_id, source_name, status, method,
                       confidence, observed_at
                FROM resource_source_matches_current
                WHERE resource_id = ?
                ORDER BY source, source_name
                """,
                [normalized],
            ).fetchall()
            azure_attempt = db.execute(
                """
                SELECT status, metric_count, message, observed_at
                FROM telemetry_resource_attempts_current
                WHERE resource_id = ? AND source = 'azure_monitor'
                """,
                [normalized],
            ).fetchone()
            logicmonitor_attempt = db.execute(
                """
                SELECT status, metric_count, message, observed_at
                FROM telemetry_resource_attempts_current
                WHERE resource_id = ? AND source = 'logicmonitor'
                """,
                [normalized],
            ).fetchone()
            rightsizing = db.execute(
                """
                SELECT computed_at, kind, status, current_sku, target_sku,
                       evidence_window_days, coverage_flag, telemetry_source,
                       cpu_p95, cpu_maximum, network_in_p95, network_out_p95,
                       metric_coverage_percent, estimated_monthly_saving,
                       currency, value_source, reason, evidence_json,
                       method_version
                FROM rightsizing_recommendations_current
                WHERE resource_id = ?
                """,
                [normalized],
            ).fetchone()
            cost_rows = db.execute(
                """
                SELECT usage_date, cost_type, sum(amount),
                       any_value(NULLIF(currency, ''))
                FROM daily_cost_history
                WHERE resource_id = ?
                  AND usage_date >= current_date - INTERVAL 35 DAY
                  AND cost_type IN ('ActualCost', 'AmortizedCost')
                GROUP BY usage_date, cost_type
                ORDER BY usage_date
                """,
                [normalized],
            ).fetchall()
            sample_rows = db.execute(
                """
                SELECT source, metric, unit,
                       date_trunc('hour', observed_at) AS bucket,
                       avg(value)
                FROM telemetry_metric_samples
                WHERE resource_id = ?
                  AND observed_at >= now() - INTERVAL 7 DAY
                GROUP BY source, metric, unit, bucket
                ORDER BY source, metric, bucket
                """,
                [normalized],
            ).fetchall()
        cost_daily: dict[str, dict[str, Any]] = {}
        for usage_date, cost_type, amount, cost_currency in cost_rows:
            entry = cost_daily.setdefault(
                usage_date.isoformat(),
                {
                    "date": usage_date.isoformat(),
                    "actual": None,
                    "amortized": None,
                    "currency": cost_currency or "",
                },
            )
            key = "actual" if cost_type == "ActualCost" else "amortized"
            entry[key] = round(float(amount or 0), 2)
        sample_series: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for source, metric, unit, bucket, value in sample_rows:
            sample_series.setdefault((source, metric, unit), []).append(
                {
                    "t": bucket.isoformat(),
                    "value": round(float(value or 0), 2),
                }
            )
        return {
            "resourceId": resource_id,
            "costDaily": list(cost_daily.values()),
            "sampleSeries": [
                {
                    "source": source,
                    "metric": metric,
                    "unit": unit,
                    "points": points,
                }
                for (source, metric, unit), points in sample_series.items()
            ],
            "metrics": [
                {
                    "source": row[0],
                    "metric": row[1],
                    "unit": row[2],
                    "windowStart": row[3].isoformat(),
                    "windowEnd": row[4].isoformat(),
                    "sampleCount": row[5],
                    "coveragePercent": round(row[6], 1),
                    "average": row[7],
                    "p95": row[8],
                    "maximum": row[9],
                    "lastValue": row[10],
                    "lastObservedAt": row[11].isoformat() if row[11] else None,
                    "aggregationMethod": row[12] or "",
                    "lineage": json.loads(row[13] or "{}"),
                }
                for row in metrics
            ],
            "matches": [
                {
                    "source": row[0],
                    "sourceResourceId": row[1],
                    "sourceName": row[2],
                    "status": row[3],
                    "method": row[4],
                    "confidence": row[5],
                    "observedAt": row[6].isoformat(),
                }
                for row in matches
            ],
            "azureMonitorAttempt": {
                "status": azure_attempt[0],
                "metricCount": azure_attempt[1],
                "message": azure_attempt[2],
                "observedAt": azure_attempt[3].isoformat(),
            } if azure_attempt else None,
            "logicMonitorAttempt": {
                "status": logicmonitor_attempt[0],
                "metricCount": logicmonitor_attempt[1],
                "message": logicmonitor_attempt[2],
                "observedAt": logicmonitor_attempt[3].isoformat(),
            } if logicmonitor_attempt else None,
            "rightsizingAssessment": {
                "computedAt": rightsizing[0].isoformat(),
                "kind": rightsizing[1],
                "status": rightsizing[2],
                "currentSku": rightsizing[3],
                "targetSku": rightsizing[4],
                "evidenceWindowDays": rightsizing[5],
                "coverageFlag": rightsizing[6],
                "telemetrySource": rightsizing[7],
                "cpuP95": rightsizing[8],
                "cpuMaximum": rightsizing[9],
                "networkInP95": rightsizing[10],
                "networkOutP95": rightsizing[11],
                "metricCoveragePercent": rightsizing[12],
                "estimatedMonthlySaving": rightsizing[13],
                "currency": rightsizing[14],
                "valueSource": rightsizing[15],
                "reason": rightsizing[16],
                "evidence": json.loads(rightsizing[17] or "{}"),
                "methodVersion": rightsizing[18],
            } if rightsizing else None,
        }

    def compute_rightsizing_recommendations(
        self,
        run_id: str,
        *,
        minimum_window_days: int = 14,
        minimum_coverage_percent: float = 70,
        idle_cpu_p95: float = 5,
        idle_cpu_maximum: float = 20,
        idle_network_p95_bytes: float = 52_428_800,
        review_cpu_p95: float = 30,
        memory_review_percent: float = 80,
        cpu_disagreement_percent: float = 20,
    ) -> int:
        computed_at = utc_now()
        with self.connect(read_only=True) as db:
            resources = db.execute(
                """
                WITH advisor AS (
                    SELECT * EXCLUDE (advisor_rank)
                    FROM (
                        SELECT
                            resource_id,
                            recommended_sku,
                            savings_amount,
                            annual_savings_amount,
                            savings_currency,
                            row_number() OVER (
                                PARTITION BY resource_id
                                ORDER BY observed_at DESC, recommendation_id
                            ) AS advisor_rank
                        FROM advisor_recommendations_current
                        WHERE lower(problem || ' ' || solution)
                            LIKE '%underutilized%virtual machine%'
                    )
                    WHERE advisor_rank = 1
                ),
                cost_by_type AS (
                    SELECT
                        resource_id,
                        cost_type,
                        sum(amount) AS amount,
                        max(currency) AS currency,
                        max(period_start) AS period_start,
                        max(period_end) AS period_end,
                        arg_max(snapshot_id, observed_at) AS cost_snapshot_id
                    FROM costs_current
                    WHERE resource_id <> ''
                    GROUP BY resource_id, cost_type
                ),
                resource_cost AS (
                    SELECT * EXCLUDE (cost_rank)
                    FROM (
                        SELECT *,
                            row_number() OVER (
                                PARTITION BY resource_id
                                ORDER BY CASE cost_type
                                    WHEN 'AmortizedCost' THEN 1 ELSE 2 END
                            ) AS cost_rank
                        FROM cost_by_type
                    )
                    WHERE cost_rank = 1
                ),
                logicmonitor AS (
                    SELECT resource_id, TRUE AS matched
                    FROM resource_source_matches_current
                    WHERE source = 'logicmonitor' AND status = 'matched'
                    GROUP BY resource_id
                )
                SELECT
                    resource.resource_id,
                    resource.name,
                    resource.subscription_id,
                    resource.subscription_name,
                    resource.resource_group,
                    resource.region,
                    resource.sku,
                    COALESCE(advisor.recommended_sku, ''),
                    advisor.savings_amount,
                    advisor.annual_savings_amount,
                    COALESCE(
                        NULLIF(advisor.savings_currency, ''),
                        cost.currency,
                        ''
                    ),
                    cost.amount,
                    COALESCE(cost.cost_type, ''),
                    cost.period_start,
                    cost.period_end,
                    COALESCE(cost.cost_snapshot_id, ''),
                    COALESCE(logicmonitor.matched, FALSE),
                    valuation.monthly_gross,
                    COALESCE(NULLIF(valuation.currency, ''), ''),
                    COALESCE(valuation.value_source, '')
                FROM resources_current AS resource
                LEFT JOIN advisor
                  ON advisor.resource_id = lower(resource.resource_id)
                LEFT JOIN resource_cost AS cost
                  ON cost.resource_id = lower(resource.resource_id)
                LEFT JOIN logicmonitor
                  ON logicmonitor.resource_id = lower(resource.resource_id)
                LEFT JOIN opportunity_valuation_current AS valuation
                  ON valuation.resource_id = lower(resource.resource_id)
                 AND valuation.opportunity_type = 'compute_shutdown'
                WHERE resource.resource_type =
                    'microsoft.compute/virtualmachines'
                """
            ).fetchall()
            metric_rows = db.execute(
                """
                SELECT
                    resource_id, source, metric, window_start, window_end,
                    sample_count, coverage_percent, average, p95, maximum,
                    unit, aggregation_method, lineage_json
                FROM telemetry_metric_summaries_current
                WHERE source IN ('azure_monitor', 'logicmonitor')
                """
            ).fetchall()

        telemetry: dict[str, dict[str, dict[str, Any]]] = {}
        for metric in metric_rows:
            telemetry.setdefault(metric[0], {}).setdefault(metric[1], {})[
                str(metric[2]).lower()
            ] = {
                "windowStart": metric[3],
                "windowEnd": metric[4],
                "sampleCount": metric[5],
                "coverage": metric[6],
                "average": metric[7],
                "p95": metric[8],
                "maximum": metric[9],
                "unit": metric[10],
                "aggregationMethod": metric[11] or "",
                "lineage": json.loads(metric[12] or "{}"),
            }

        rows = []
        for row in resources:
            resource_id = str(row[0]).lower()
            source_metrics = telemetry.get(resource_id, {})
            evidence_sources: dict[str, Any] = {}
            cpu_values: list[float] = []
            cpu_maxima: list[float] = []
            cpu_coverages: list[float] = []
            window_starts = []
            window_ends = []
            memory_values: list[float] = []
            network_in_values: list[float] = []
            network_out_values: list[float] = []
            active_sources = []
            for source in ("azure_monitor", "logicmonitor"):
                metrics = source_metrics.get(source, {})
                cpu = metrics.get("percentage cpu")
                if not cpu or cpu.get("p95") is None:
                    continue
                active_sources.append(source)
                cpu_values.append(float(cpu["p95"]))
                if cpu.get("maximum") is not None:
                    cpu_maxima.append(float(cpu["maximum"]))
                if cpu.get("coverage") is not None:
                    cpu_coverages.append(float(cpu["coverage"]))
                if cpu.get("windowStart"):
                    window_starts.append(cpu["windowStart"])
                if cpu.get("windowEnd"):
                    window_ends.append(cpu["windowEnd"])
                memory = metrics.get("memory used percentage")
                network_in = metrics.get("network in total")
                network_out = metrics.get("network out total")
                if memory and memory.get("p95") is not None:
                    memory_values.append(float(memory["p95"]))
                if network_in and network_in.get("p95") is not None:
                    network_in_values.append(float(network_in["p95"]))
                if network_out and network_out.get("p95") is not None:
                    network_out_values.append(float(network_out["p95"]))
                evidence_sources[source] = {
                    metric_name: {
                        "p95": value.get("p95"),
                        "maximum": value.get("maximum"),
                        "coveragePercent": value.get("coverage"),
                        "windowStart": value["windowStart"].isoformat()
                        if value.get("windowStart") else None,
                        "windowEnd": value["windowEnd"].isoformat()
                        if value.get("windowEnd") else None,
                        "aggregationMethod": value.get("aggregationMethod", ""),
                        "lineage": value.get("lineage", {}),
                    }
                    for metric_name, value in metrics.items()
                }
            complete_memory = (
                max(memory_values) if len(memory_values) == len(active_sources)
                and active_sources else None
            )
            complete_network_in = (
                max(network_in_values)
                if len(network_in_values) == len(active_sources)
                and active_sources else None
            )
            complete_network_out = (
                max(network_out_values)
                if len(network_out_values) == len(active_sources)
                and active_sources else None
            )
            cpu_delta = (
                max(cpu_values) - min(cpu_values)
                if len(cpu_values) > 1 else 0
            )
            assessment = assess_resource(
                attempt_status="covered" if active_sources else "not_attempted",
                window_start=max(window_starts) if window_starts else None,
                window_end=min(window_ends) if window_ends else None,
                cpu_p95=max(cpu_values) if cpu_values else None,
                cpu_maximum=max(cpu_maxima) if cpu_maxima else None,
                cpu_coverage=min(cpu_coverages) if cpu_coverages else None,
                memory_p95=complete_memory,
                network_in_p95=complete_network_in,
                network_out_p95=complete_network_out,
                source_disagreement=cpu_delta > cpu_disagreement_percent,
                advisor_target_sku=row[7],
                advisor_monthly_savings=row[8],
                minimum_window_days=minimum_window_days,
                minimum_coverage_percent=minimum_coverage_percent,
                idle_cpu_p95=idle_cpu_p95,
                idle_cpu_maximum=idle_cpu_maximum,
                idle_network_p95_bytes=idle_network_p95_bytes,
                review_cpu_p95=review_cpu_p95,
                memory_review_percent=memory_review_percent,
            )
            saving = None
            value_source = ""
            if assessment["status"] == "candidate":
                if assessment["kind"] == "resize":
                    saving = (
                        float(row[17])
                        if row[17] is not None
                        else float(row[8])
                        if row[8] is not None
                        else round(float(row[9]) / 12, 2)
                        if row[9] is not None
                        else None
                    )
                    value_source = row[19] or "azure_advisor"
                elif (
                    assessment["kind"] == "shutdown"
                    and row[11] is not None
                    and row[13]
                    and row[14]
                ):
                    saving = monthly_run_rate(row[11], row[13], row[14])
                    value_source = (
                        "amortized_cost_run_rate"
                        if row[12] == "AmortizedCost"
                        else "actual_cost_run_rate"
                    )
            evidence = {
                "attemptStatus": "covered" if active_sources else "not_attempted",
                "sourcesUsed": active_sources,
                "sourceEvidence": evidence_sources,
                "cpuP95Delta": round(cpu_delta, 2),
                "logicMonitorMatched": bool(row[16]),
                "logicMonitorMetricsUsed": "logicmonitor" in active_sources,
                "advisorTargetSku": row[7],
                "costSnapshotId": row[15],
                "costType": row[12],
                "costPeriodStart": row[13].isoformat() if row[13] else None,
                "costPeriodEnd": row[14].isoformat() if row[14] else None,
                "governedValuationSource": row[19],
                "thresholds": {
                    "minimumWindowDays": minimum_window_days,
                    "minimumCoveragePercent": minimum_coverage_percent,
                    "idleCpuP95": idle_cpu_p95,
                    "idleCpuMaximum": idle_cpu_maximum,
                    "idleNetworkP95Bytes": idle_network_p95_bytes,
                    "reviewCpuP95": review_cpu_p95,
                    "memoryReviewPercent": memory_review_percent,
                    "cpuDisagreementPercent": cpu_disagreement_percent,
                },
            }
            rows.append(
                [
                    run_id,
                    computed_at,
                    resource_id,
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    assessment["kind"],
                    assessment["status"],
                    row[6],
                    assessment["targetSku"],
                    assessment["evidenceWindowDays"],
                    assessment["coverageFlag"],
                    "+".join(active_sources),
                    assessment["cpuP95"],
                    assessment["cpuMaximum"],
                    assessment["networkInP95"],
                    assessment["networkOutP95"],
                    assessment["metricCoveragePercent"],
                    saving,
                    row[18] or row[10],
                    value_source,
                    assessment["reason"],
                    json_value(evidence),
                    RIGHTSIZING_METHOD_VERSION,
                ]
            )

        with self.connect() as db:
            db.execute(
                "DELETE FROM rightsizing_recommendation_snapshots WHERE run_id = ?",
                [run_id],
            )
            if rows:
                db.executemany(
                    """
                    INSERT INTO rightsizing_recommendation_snapshots VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    rows,
                )
        return len(rows)

    def ensure_rightsizing_recommendations(
        self,
        **thresholds: Any,
    ) -> int:
        with self.connect(read_only=True) as db:
            latest_run = db.execute(
                """
                SELECT arg_max(id, completed_at)
                FROM telemetry_runs
                WHERE source IN ('azure_monitor', 'logicmonitor')
                  AND status = 'succeeded'
                """
            ).fetchone()[0]
            existing = db.execute(
                """
                SELECT count(*)
                FROM rightsizing_recommendation_snapshots
                WHERE run_id = ? AND method_version = ?
                """,
                [latest_run, RIGHTSIZING_METHOD_VERSION],
            ).fetchone()[0] if latest_run else 0
        if not latest_run or existing:
            return 0
        return self.compute_rightsizing_recommendations(
            latest_run,
            **thresholds,
        )

    def rightsizing_dossier(self, resource_id: str) -> dict[str, Any]:
        """Everything the estate knows about one VM as a resize candidate.

        Built for the deep Review-with-Flux workflow: telemetry (all
        sources, including guest memory), the governed assessment with its
        evidence, per-resource cost history, FOCUS pricing-category and
        commitment-coverage exposure, retail price comparison between the
        current and target SKU in the VM's own region, and the human plan
        assignment. One bounded call instead of the model piecing together
        five tools.
        """
        normalized = resource_id.lower()
        dossier = self.resource_telemetry(resource_id)
        with self.connect(read_only=True) as db:
            resource = db.execute(
                """
                SELECT name, subscription_id, subscription_name,
                       resource_group, region, sku, tags_json
                FROM resources_current
                WHERE lower(resource_id) = ?
                """,
                [normalized],
            ).fetchone()
            focus = db.execute(
                """
                SELECT pricing_category,
                       CASE WHEN commitment_discount_id <> ''
                            THEN commitment_discount_type
                            ELSE 'None' END AS commitment,
                       round(sum(billed_cost), 2),
                       round(sum(effective_cost), 2)
                FROM focus_cost_charges
                WHERE lower(resource_id) = ?
                  AND charge_period_start >= now() - INTERVAL 90 DAY
                GROUP BY 1, 2
                ORDER BY 4 DESC
                """,
                [normalized],
            ).fetchall()
            assessment = dossier.get("rightsizingAssessment") or {}
            current_sku = str(
                assessment.get("currentSku")
                or (resource[5] if resource else "")
                or ""
            )
            target_sku = str(assessment.get("targetSku") or "")
            region = str(resource[4] if resource else "").lower()
            prices = []
            if region and (current_sku or target_sku):
                skus = [sku for sku in {current_sku, target_sku} if sku]
                placeholders = ", ".join("?" for _ in skus)
                prices = db.execute(
                    f"""
                    SELECT arm_sku_name, price_profile, currency,
                           hourly_price, monthly_price
                    FROM retail_prices_current
                    WHERE lower(arm_region_name) = ?
                      AND arm_sku_name IN ({placeholders})
                    ORDER BY arm_sku_name, price_profile
                    """,
                    [region, *skus],
                ).fetchall()
        plan = None
        try:
            board = self.rightsizing_plan_board()
            for vm in board.get("vms", []):
                # vmKey is the lowercased resource ID.
                if str(vm.get("vmKey") or "") == normalized:
                    assignment = board.get("assignments", {}).get(
                        vm.get("vmKey"), {}
                    )
                    plan = {
                        "bucketKey": assignment.get("bucketKey")
                        or ("__nodata__" if vm.get("noData") else "__unassigned__"),
                        "decision": assignment.get("decision") or "Pending",
                        "note": assignment.get("note") or "",
                    }
                    break
        except Exception:
            plan = None
        dossier.update(
            {
                "resource": {
                    "name": resource[0],
                    "subscriptionId": resource[1],
                    "subscriptionName": resource[2],
                    "resourceGroup": resource[3],
                    "region": resource[4],
                    "sku": resource[5],
                    "tags": json.loads(resource[6] or "{}"),
                }
                if resource
                else None,
                "focusExposure90d": [
                    {
                        "pricingCategory": row[0],
                        "commitmentDiscountType": row[1],
                        "billedCost": row[2],
                        "effectiveCost": row[3],
                    }
                    for row in focus
                ],
                "retailPriceComparison": [
                    {
                        "sku": row[0],
                        "priceProfile": row[1],
                        "currency": row[2],
                        "hourlyPrice": row[3],
                        "monthlyPrice": row[4],
                    }
                    for row in prices
                ],
                "planAssignment": plan,
            }
        )
        return dossier

    def rightsizing_recommendations(
        self,
        *,
        status: str = "",
        subscription_id: str = "",
        resource_id: str = "",
        limit: int = 250,
        offset: int = 0,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if subscription_id:
            conditions.append("subscription_id = ?")
            params.append(subscription_id)
        if resource_id:
            conditions.append("resource_id = ?")
            params.append(resource_id.lower())
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect(read_only=True) as db:
            total = db.execute(
                f"SELECT count(*) FROM rightsizing_recommendations_current {where}",
                params,
            ).fetchone()[0]
            rows = db.execute(
                f"""
                SELECT
                    run_id, computed_at, resource_id, resource_name,
                    subscription_id, subscription_name, resource_group, region,
                    kind, status, current_sku, target_sku,
                    evidence_window_days, coverage_flag, telemetry_source,
                    cpu_p95, cpu_maximum, network_in_p95, network_out_p95,
                    metric_coverage_percent, estimated_monthly_saving,
                    currency, value_source, reason, evidence_json, method_version
                FROM rightsizing_recommendations_current
                {where}
                ORDER BY
                    CASE status WHEN 'candidate' THEN 1
                        WHEN 'needs_review' THEN 2
                        WHEN 'target_rate_unavailable' THEN 3
                        WHEN 'warming_up' THEN 4
                        WHEN 'partial_telemetry' THEN 5
                        WHEN 'insufficient_telemetry' THEN 6 ELSE 7 END,
                    estimated_monthly_saving DESC NULLS LAST,
                    resource_name
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            summary = db.execute(
                """
                SELECT
                    count(*),
                    count(*) FILTER (WHERE status = 'candidate'),
                    count(*) FILTER (WHERE status = 'warming_up'),
                    count(*) FILTER (WHERE status = 'needs_review'),
                    count(*) FILTER (
                        WHERE status IN (
                            'insufficient_telemetry', 'partial_telemetry'
                        )
                    ),
                    count(*) FILTER (WHERE coverage_flag = 'covered'),
                    COALESCE(sum(estimated_monthly_saving) FILTER (
                        WHERE status = 'candidate'
                    ), 0)
                FROM rightsizing_recommendations_current
                """
            ).fetchone()
        return {
            "items": [
                {
                    "runId": row[0],
                    "computedAt": row[1].isoformat(),
                    "resourceId": row[2],
                    "resourceName": row[3],
                    "subscriptionId": row[4],
                    "subscriptionName": row[5],
                    "resourceGroup": row[6],
                    "region": row[7],
                    "kind": row[8],
                    "status": row[9],
                    "currentSku": row[10],
                    "targetSku": row[11],
                    "evidenceWindowDays": row[12],
                    "coverageFlag": row[13],
                    "telemetrySource": row[14],
                    "cpuP95": row[15],
                    "cpuMaximum": row[16],
                    "networkInP95": row[17],
                    "networkOutP95": row[18],
                    "metricCoveragePercent": row[19],
                    "estimatedMonthlySaving": row[20],
                    "currency": row[21],
                    "valueSource": row[22],
                    "reason": row[23],
                    "evidence": json.loads(row[24]) if row[24] else {},
                    "methodVersion": row[25],
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "summary": {
                "virtualMachines": summary[0],
                "candidates": summary[1],
                "warmingUp": summary[2],
                "needsReview": summary[3],
                "insufficient": summary[4],
                "covered": summary[5],
                "estimatedMonthlySaving": summary[6],
            },
        }

    def aged_snapshots(
        self,
        *,
        age_days: int = 30,
        limit: int = 2000,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return aged snapshot findings for deletion pre-check review only."""
        with self.connect(read_only=True) as db:
            rows = db.execute(
                """
                SELECT finding.resource_id,
                       COALESCE(resource.name, json_extract_string(finding.evidence_json, '$.resourceName'), ''),
                       COALESCE(resource.subscription_name, finding.subscription_name),
                       COALESCE(resource.resource_group, finding.resource_group),
                       COALESCE(resource.region, finding.region),
                       json_extract_string(finding.evidence_json, '$.timeCreated'),
                       COALESCE(resource.sku, json_extract_string(finding.evidence_json, '$.skuName'), ''),
                       finding.observed_at
                FROM rule_opportunities_current AS finding
                LEFT JOIN resources_current AS resource
                  ON lower(resource.resource_id) = finding.resource_id
                WHERE finding.rule_id = 'aged_snapshot'
                  AND date_diff('day', TRY_CAST(json_extract_string(finding.evidence_json, '$.timeCreated') AS TIMESTAMP), current_timestamp) >= ?
                ORDER BY 6 ASC NULLS LAST, finding.resource_id
                LIMIT ? OFFSET ?
                """,
                [age_days, limit, offset],
            ).fetchall()
            total = db.execute(
                """
                SELECT count(*) FROM rule_opportunities_current AS finding
                WHERE finding.rule_id = 'aged_snapshot'
                  AND date_diff('day', TRY_CAST(json_extract_string(finding.evidence_json, '$.timeCreated') AS TIMESTAMP), current_timestamp) >= ?
                """,
                [age_days],
            ).fetchone()[0]
        return {
            "items": [
                {
                    "resourceId": row[0], "resourceName": row[1] or "",
                    "subscriptionName": row[2] or "", "resourceGroup": row[3] or "",
                    "region": row[4] or "", "timeCreated": row[5],
                    "ageDaysThreshold": age_days, "sku": row[6] or "",
                    "observedAt": row[7].isoformat(),
                    "prechecksRequired": [
                        "Approved backup/recovery retention",
                        "Legal or regulatory retention",
                        "Active restore or DR dependency",
                        "Owner approval and deletion record",
                    ],
                }
                for row in rows
            ],
            "total": total, "ageDays": age_days, "limit": limit,
            "offset": offset, "reviewOnly": True,
        }

    def opportunities(
        self,
        *,
        search: str = "",
        resource_id: str = "",
        resource_type: str = "",
        subscription_id: str = "",
        region: str = "",
        source: str = "",
        category: str = "",
        confidence: str = "",
        actionability: str = "",
        include_governance: bool = False,
        sort: str = "impact",
        direction: str = "desc",
        limit: int = 250,
        offset: int = 0,
    ) -> dict[str, Any]:
        base = """
            WITH resource_costs AS (
                SELECT
                    resource_id,
                    SUM(CASE WHEN cost_type = 'ActualCost' THEN amount END) AS actual_cost,
                    max(currency) AS currency
                FROM costs_current
                WHERE resource_id <> ''
                GROUP BY resource_id
            ),
            premium_disk_metrics AS (
                SELECT
                    lower(resource_id) AS resource_id,
                    max(CASE WHEN lower(metric) = 'disk read operations/sec' THEN p95 END) AS read_iops_p95,
                    max(CASE WHEN lower(metric) = 'disk write operations/sec' THEN p95 END) AS write_iops_p95,
                    max(CASE WHEN lower(metric) = 'disk read bytes/sec' THEN p95 END) AS read_bytes_p95,
                    max(CASE WHEN lower(metric) = 'disk write bytes/sec' THEN p95 END) AS write_bytes_p95,
                    min(coverage_percent) AS coverage_percent,
                    min(window_start) AS window_start,
                    max(window_end) AS window_end,
                    count(DISTINCT lower(metric)) AS metric_count,
                    count(DISTINCT lower(metric)) FILTER (
                        WHERE lower(COALESCE(json_extract_string(lineage_json, '$.metricScope'), '')) = 'disk'
                    ) AS disk_scoped_metric_count
                FROM telemetry_metric_summaries_current
                WHERE lower(metric) IN (
                    'disk read operations/sec', 'disk write operations/sec',
                    'disk read bytes/sec', 'disk write bytes/sec'
                )
                GROUP BY lower(resource_id)
            ),
            opportunity_raw AS (
                SELECT
                    'inventory:' || resource.resource_id || ':' ||
                        resource.opportunity_kind AS opportunity_id,
                    'inventory_rule' AS source,
                    resource.opportunity_kind AS kind,
                    'Inventory' AS category,
                    '' AS impact,
                    'Review' AS confidence,
                    resource.name AS title,
                    resource.opportunity_reason AS reason,
                    resource.resource_id,
                    '' AS related_resource_id,
                    resource.name AS resource_name,
                    resource.resource_type,
                    resource.subscription_id,
                    resource.subscription_name,
                    resource.resource_group,
                    resource.region,
                    resource.estimated_monthly_savings,
                    NULL::DOUBLE AS annual_savings_amount,
                    COALESCE(cost.currency, '') AS savings_currency,
                    COALESCE(cost.actual_cost, resource.estimated_monthly_cost) AS actual_cost,
                    '' AS current_sku,
                    '' AS recommended_sku,
                    NULL::TIMESTAMPTZ AS last_updated,
                    '' AS learn_more_link,
                    resource.observed_at,
                    resource.opportunity_kind AS family
                FROM resources_current AS resource
                LEFT JOIN resource_costs AS cost
                  ON cost.resource_id = lower(resource.resource_id)
                WHERE resource.opportunity_kind IS NOT NULL

                UNION ALL

                SELECT
                    'flux:' || resource.resource_id || ':premium_disk_underutilized_review' AS opportunity_id,
                    'flux_intelligence' AS source,
                    'premium_disk_underutilized_review' AS kind,
                    'Cost' AS category,
                    'Medium' AS impact,
                    'Review' AS confidence,
                    resource.name || ': Attached Premium disk utilization review' AS title,
                    'Sustained telemetry is below the Premium disk review thresholds '
                        || '(IOPS p95 <= __PREMIUM_IOPS__, throughput p95 <= __PREMIUM_THROUGHPUT__ bytes/sec) '
                        || 'over at least __PREMIUM_WINDOW__ days with at least __PREMIUM_COVERAGE__% metric coverage. '
                        || 'Validate latency, capacity, bursting, and recovery requirements before changing storage tier.' AS reason,
                    resource.resource_id,
                    '' AS related_resource_id,
                    resource.name AS resource_name,
                    resource.resource_type,
                    resource.subscription_id,
                    resource.subscription_name,
                    resource.resource_group,
                    resource.region,
                    NULL::DOUBLE AS estimated_monthly_savings,
                    NULL::DOUBLE AS annual_savings_amount,
                    COALESCE(cost.currency, '') AS savings_currency,
                    COALESCE(cost.actual_cost, resource.estimated_monthly_cost) AS actual_cost,
                    resource.sku AS current_sku,
                    '' AS recommended_sku,
                    NULL::TIMESTAMPTZ AS last_updated,
                    '' AS learn_more_link,
                    metrics.window_end AS observed_at,
                    'premium_disk_underutilized_review' AS family
                FROM resources_current AS resource
                JOIN premium_disk_metrics AS metrics
                  ON metrics.resource_id = lower(resource.resource_id)
                LEFT JOIN resource_costs AS cost
                  ON cost.resource_id = lower(resource.resource_id)
                WHERE lower(resource.resource_type) = 'microsoft.compute/disks'
                  AND lower(resource.sku) LIKE '%premium%'
                  AND COALESCE(resource.managed_by, '') <> ''
                  AND metrics.metric_count = 4
                  AND metrics.disk_scoped_metric_count = 4
                  AND date_diff('day', metrics.window_start, metrics.window_end) >= __PREMIUM_WINDOW__
                  AND metrics.coverage_percent >= __PREMIUM_COVERAGE__
                  AND COALESCE(metrics.read_iops_p95, 0) + COALESCE(metrics.write_iops_p95, 0) <= __PREMIUM_IOPS__
                  AND COALESCE(metrics.read_bytes_p95, 0) + COALESCE(metrics.write_bytes_p95, 0) <= __PREMIUM_THROUGHPUT__

                UNION ALL

                SELECT
                    'advisor:' || advisor.recommendation_id AS opportunity_id,
                    'azure_advisor' AS source,
                    'advisor_' || lower(replace(advisor.category, ' ', '_')) AS kind,
                    advisor.category,
                    advisor.impact,
                    '' AS confidence,
                    CASE
                        WHEN NULLIF(resource.name, '') IS NOT NULL
                            THEN resource.name
                        WHEN lower(advisor.resource_id) =
                            '/subscriptions/' || lower(advisor.subscription_id)
                            THEN COALESCE(
                                NULLIF(advisor.solution, ''),
                                NULLIF(advisor.problem, ''),
                                'Review Azure Advisor recommendation'
                            ) ||
                            CASE
                                WHEN COALESCE(
                                    json_extract_string(
                                        advisor.raw_json,
                                        '$._fluxActionContext'
                                    ),
                                    ''
                                ) <> ''
                                THEN ' — ' || json_extract_string(
                                    advisor.raw_json,
                                    '$._fluxActionContext'
                                )
                                ELSE ''
                            END
                        WHEN advisor.resource_id <> ''
                            THEN regexp_extract(advisor.resource_id, '/([^/]+)$', 1)
                        ELSE COALESCE(
                            NULLIF(advisor.subscription_name, ''),
                            'Azure Advisor recommendation'
                        )
                    END AS title,
                    concat_ws(
                        ' ',
                        CASE
                            WHEN advisor.problem <> ''
                              AND advisor.solution <> ''
                                THEN advisor.problem || ' ' || advisor.solution
                            ELSE COALESCE(
                                NULLIF(advisor.problem, ''),
                                advisor.solution
                            )
                        END,
                        CASE
                            WHEN COALESCE(
                                json_extract_string(
                                    advisor.raw_json,
                                    '$._fluxActionContext'
                                ),
                                ''
                            ) <> ''
                            THEN 'Scope details: ' || json_extract_string(
                                advisor.raw_json,
                                '$._fluxActionContext'
                            ) || '.'
                            ELSE ''
                        END
                    ) AS reason,
                    advisor.resource_id,
                    '' AS related_resource_id,
                    CASE
                        WHEN NULLIF(resource.name, '') IS NOT NULL
                            THEN resource.name
                        WHEN lower(advisor.resource_id) =
                            '/subscriptions/' || lower(advisor.subscription_id)
                            THEN COALESCE(
                                NULLIF(advisor.subscription_name, ''),
                                advisor.subscription_id,
                                'Azure'
                            ) || ' subscription scope'
                        WHEN advisor.resource_id <> ''
                            THEN regexp_extract(advisor.resource_id, '/([^/]+)$', 1)
                        ELSE ''
                    END AS resource_name,
                    COALESCE(NULLIF(resource.resource_type, ''), advisor.resource_type) AS resource_type,
                    advisor.subscription_id,
                    COALESCE(
                        NULLIF(resource.subscription_name, ''),
                        advisor.subscription_name
                    ) AS subscription_name,
                    COALESCE(resource.resource_group, '') AS resource_group,
                    COALESCE(resource.region, '') AS region,
                    advisor.savings_amount AS estimated_monthly_savings,
                    advisor.annual_savings_amount,
                    advisor.savings_currency,
                    cost.actual_cost,
                    COALESCE(
                        NULLIF(advisor.current_sku, ''),
                        resource.sku,
                        ''
                    ) AS current_sku,
                    advisor.recommended_sku,
                    advisor.last_updated,
                    advisor.learn_more_link,
                    advisor.observed_at,
                    CASE
                        WHEN lower(advisor.problem || ' ' || advisor.solution)
                            LIKE '%unattached%disk%' THEN 'unattached_disk'
                        WHEN lower(advisor.problem || ' ' || advisor.solution)
                            LIKE '%underutilized%virtual machine%' THEN 'compute_shutdown'
                        WHEN lower(advisor.problem || ' ' || advisor.solution)
                            LIKE '%app service plan%' THEN 'empty_app_service_plan'
                        ELSE 'advisor_' || lower(replace(advisor.category, ' ', '_'))
                    END AS family
                FROM advisor_recommendations_current AS advisor
                LEFT JOIN resources_current AS resource
                  ON lower(resource.resource_id) = advisor.resource_id
                LEFT JOIN resource_costs AS cost
                  ON cost.resource_id = advisor.resource_id

                UNION ALL

                SELECT
                    'flux:' || finding.finding_id AS opportunity_id,
                    finding.source,
                    finding.rule_id AS kind,
                    finding.category,
                    finding.impact,
                    finding.confidence,
                    finding.title,
                    finding.reason,
                    finding.resource_id,
                    finding.related_resource_id,
                    COALESCE(resource.name, '') AS resource_name,
                    COALESCE(
                        NULLIF(resource.resource_type, ''),
                        finding.resource_type
                    ) AS resource_type,
                    finding.subscription_id,
                    COALESCE(
                        NULLIF(resource.subscription_name, ''),
                        finding.subscription_name
                    ) AS subscription_name,
                    COALESCE(
                        NULLIF(resource.resource_group, ''),
                        finding.resource_group
                    ) AS resource_group,
                    COALESCE(NULLIF(resource.region, ''), finding.region) AS region,
                    finding.estimated_monthly_savings,
                    NULL::DOUBLE AS annual_savings_amount,
                    COALESCE(
                        NULLIF(finding.savings_currency, ''),
                        cost.currency,
                        ''
                    ) AS savings_currency,
                    cost.actual_cost,
                    '' AS current_sku,
                    '' AS recommended_sku,
                    NULL::TIMESTAMPTZ AS last_updated,
                    '' AS learn_more_link,
                    finding.observed_at,
                    CASE finding.rule_id
                        WHEN 'stopped_allocated_vm' THEN 'compute_shutdown'
                        WHEN 'deallocated_vm_residual_cost' THEN 'compute_shutdown'
                        WHEN 'empty_paid_app_service_plan' THEN 'empty_app_service_plan'
                        ELSE finding.rule_id
                    END AS family
                FROM rule_opportunities_current AS finding
                LEFT JOIN resources_current AS resource
                  ON lower(resource.resource_id) = finding.resource_id
                LEFT JOIN resource_costs AS cost
                  ON cost.resource_id = finding.resource_id
            ),
            corroboration AS (
                SELECT
                    resource_id,
                    family,
                    count(DISTINCT source) AS source_count,
                    string_agg(DISTINCT source, ',') AS source_set
                FROM opportunity_raw
                WHERE resource_id <> ''
                GROUP BY resource_id, family
            ),
            ranked AS (
                SELECT
                    opportunity_raw.*,
                    COALESCE(corroboration.source_count, 1) AS source_count,
                    COALESCE(corroboration.source_set, opportunity_raw.source) AS source_set,
                    row_number() OVER (
                        PARTITION BY
                            opportunity_raw.source,
                            opportunity_raw.resource_id,
                            opportunity_raw.family,
                            opportunity_raw.title,
                            opportunity_raw.current_sku,
                            opportunity_raw.recommended_sku
                        ORDER BY
                            opportunity_raw.estimated_monthly_savings DESC
                                NULLS LAST,
                            opportunity_raw.observed_at DESC,
                            opportunity_raw.opportunity_id
                    ) AS semantic_rank,
                    row_number() OVER (
                        PARTITION BY opportunity_raw.resource_id, opportunity_raw.family
                        ORDER BY CASE opportunity_raw.source
                            WHEN 'azure_advisor' THEN 1
                            WHEN 'flux_intelligence' THEN 2
                            ELSE 3
                        END
                    ) AS source_rank
                FROM opportunity_raw
                LEFT JOIN corroboration
                  ON corroboration.resource_id = opportunity_raw.resource_id
                 AND corroboration.family = opportunity_raw.family
            ),
            opportunity_base AS (
                SELECT * EXCLUDE (source_rank, semantic_rank)
                FROM ranked
                WHERE semantic_rank = 1
                  AND (source_count = 1 OR source_rank = 1)
            ),
            opportunity_scored AS (
                SELECT
                    opportunity_base.* EXCLUDE (confidence),
                    COALESCE(
                        score.confidence_label,
                        opportunity_base.confidence
                    ) AS confidence,
                    score.confidence AS confidence_score,
                    score.first_seen,
                    score.last_seen,
                    CASE
                        WHEN score.first_seen IS NOT NULL
                            THEN date_diff('day', score.first_seen, current_timestamp)
                        ELSE NULL
                    END AS age_days,
                    score.consecutive_count,
                    score.reappeared_after_remediation,
                    score.factors_json,
                    score.method_version,
                    valuation.valuation_status,
                    valuation.monthly_gross,
                    valuation.monthly_risk_adjusted,
                    valuation.currency AS valuation_currency,
                    valuation.value_source,
                    valuation.valuation_basis,
                    valuation.cost_snapshot_id,
                    valuation.cost_type,
                    valuation.cost_period_start,
                    valuation.cost_period_end,
                    valuation.method_version AS valuation_method_version,
                    valuation.computed_at AS valuation_computed_at,
                    valuation.current_monthly_cost,
                    valuation.target_monthly_cost,
                    valuation.current_cost_basis,
                    valuation.target_price_basis,
                    valuation.target_price_snapshot_id,
                    valuation.target_price_status,
                    valuation.target_hourly_price,
                    valuation.target_hours_per_month,
                    valuation.target_meter_id,
                    valuation.target_meter_name,
                    valuation.target_product_name,
                    valuation.target_price_effective_start,
                    valuation.operating_system,
                    valuation.license_model
                FROM opportunity_base
                LEFT JOIN opportunity_confidence_current AS score
                  ON score.resource_id = opportunity_base.resource_id
                 AND score.opportunity_type = opportunity_base.family
                LEFT JOIN opportunity_valuation_current AS valuation
                  ON valuation.resource_id = opportunity_base.resource_id
                 AND valuation.opportunity_type = opportunity_base.family
            ),
            opportunity_actionable AS (
                SELECT
                    opportunity_scored.*,
                    CASE
                        WHEN lower(category) = 'governance'
                          OR (
                            source = 'azure_advisor'
                            AND lower(category) NOT IN ('cost', 'performance')
                            )
                            THEN 'governance_review'
                        WHEN observed_at < current_timestamp - (CASE family __RULE_FRESHNESS_CASE__ ELSE __INTELLIGENCE_STALE_DAYS__ END) * INTERVAL '1 day'
                            THEN 'evidence_needed'
                        WHEN resource_id = concat(
                            '/subscriptions/', lower(subscription_id)
                        )
                            THEN 'portfolio_review'
                        WHEN resource_id = '' OR resource_name = ''
                            THEN 'evidence_needed'
                        WHEN source_count > 1
                          OR COALESCE(monthly_risk_adjusted, 0) > 0
                          OR COALESCE(estimated_monthly_savings, 0) > 0
                          OR (
                            COALESCE(actual_cost, 0) > 0
                            AND (
                                lower(impact) = 'high'
                                OR COALESCE(confidence_score, 0) >= 0.6
                            )
                          )
                            THEN 'actionable_now'
                        ELSE 'evidence_needed'
                    END AS actionability,
                    CASE
                        WHEN observed_at < current_timestamp - (CASE family __RULE_FRESHNESS_CASE__ ELSE __INTELLIGENCE_STALE_DAYS__ END) * INTERVAL '1 day'
                            THEN 'Evidence is older than the configured freshness window; refresh source data before acting.'
                        WHEN lower(category) = 'governance'
                            THEN 'Governance review; not a direct FinOps action.'
                        WHEN source = 'azure_advisor'
                          AND lower(category) NOT IN ('cost', 'performance')
                            THEN 'Non-FinOps Advisor category.'
                        WHEN resource_id = concat(
                            '/subscriptions/', lower(subscription_id)
                        )
                            THEN 'Subscription-level portfolio action; inspect Azure guidance before assigning work.'
                        WHEN resource_id = '' OR resource_name = ''
                            THEN 'A concrete Azure resource could not be resolved.'
                        WHEN source_count > 1
                            THEN 'Independent sources corroborate this resource action.'
                        WHEN COALESCE(monthly_risk_adjusted, 0) > 0
                            THEN 'Governed confidence-adjusted value is available.'
                        WHEN COALESCE(estimated_monthly_savings, 0) > 0
                            THEN 'The source supplied an estimated saving.'
                        WHEN COALESCE(actual_cost, 0) > 0
                          AND lower(impact) = 'high'
                            THEN 'High-impact finding has current cost exposure.'
                        WHEN COALESCE(actual_cost, 0) > 0
                          AND COALESCE(confidence_score, 0) >= 0.6
                            THEN 'Persistent evidence and current cost exposure meet the action threshold.'
                        ELSE 'More cost, telemetry, confidence, or corroboration evidence is required.'
                    END AS actionability_reason
                FROM opportunity_scored
            )
        """
        base = base.replace("__PREMIUM_IOPS__", str(settings.premium_disk_review_iops_p95))
        base = base.replace("__PREMIUM_THROUGHPUT__", str(settings.premium_disk_review_throughput_p95_bytes))
        base = base.replace("__PREMIUM_WINDOW__", str(settings.premium_disk_review_window_days))
        base = base.replace("__PREMIUM_COVERAGE__", str(settings.premium_disk_review_coverage_percent))
        base = base.replace("__INTELLIGENCE_STALE_DAYS__", str(settings.intelligence_snapshot_age_days))
        freshness_case = " ".join(
            f"WHEN '{rule}' THEN {days}"
            for rule, days in RULE_FRESHNESS_DAYS.items()
        )
        base = base.replace("__RULE_FRESHNESS_CASE__", freshness_case)
        conditions: list[str] = []
        params: list[Any] = []
        if search:
            conditions.append(
                "(title ILIKE ? OR resource_name ILIKE ? OR reason ILIKE ? "
                "OR subscription_name ILIKE ? OR resource_group ILIKE ? "
                "OR resource_type ILIKE ? OR resource_id ILIKE ?)"
            )
            token = f"%{search}%"
            params.extend([token, token, token, token, token, token, token])
        if resource_id:
            conditions.append("resource_id = ?")
            params.append(resource_id.lower())
        if resource_type:
            conditions.append("resource_type = ?")
            params.append(resource_type)
        if subscription_id:
            conditions.append("subscription_id = ?")
            params.append(subscription_id)
        if region:
            conditions.append("region = ?")
            params.append(region)
        if source:
            conditions.append("(',' || source_set || ',') LIKE ?")
            params.append(f"%,{source},%")
        if category:
            conditions.append("category = ?")
            params.append(category)
        elif not include_governance:
            conditions.append("lower(category) <> 'governance'")
            conditions.append(
                "NOT (source = 'azure_advisor' "
                "AND lower(category) NOT IN ('cost', 'performance'))"
            )
        if confidence:
            conditions.append("lower(confidence) = lower(?)")
            params.append(confidence)
        if actionability:
            conditions.append("actionability = ?")
            params.append(actionability)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sort_columns = {
            "impact": """CASE lower(impact) WHEN 'high' THEN 3
                WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END""",
            "savings": "estimated_monthly_savings",
            "valuation": "monthly_risk_adjusted",
            "cost": "actual_cost",
            "confidence": """COALESCE(confidence_score, CASE lower(confidence)
                WHEN 'high' THEN 0.8 WHEN 'medium' THEN 0.6
                WHEN 'review' THEN 0.3 ELSE 0 END)""",
            "updated": "COALESCE(last_updated, observed_at)",
            "resource": "title",
        }
        sort_expression = sort_columns.get(sort, sort_columns["impact"])
        sort_direction = "ASC" if direction.lower() == "asc" else "DESC"

        with self.connect(read_only=True) as db:
            total = db.execute(
                f"{base} SELECT COUNT(*) FROM opportunity_actionable {where}",
                params,
            ).fetchone()[0]
            rows = db.execute(
                f"""
                {base}
                SELECT
                    opportunity_id, source, kind, category, impact, confidence,
                    title, reason, resource_id, related_resource_id,
                    resource_name, resource_type, subscription_id,
                    subscription_name, resource_group, region,
                    estimated_monthly_savings, annual_savings_amount,
                    savings_currency, actual_cost, current_sku, recommended_sku,
                    last_updated, learn_more_link, observed_at, source_count,
                    source_set, family, confidence_score, first_seen, last_seen,
                    age_days, consecutive_count, reappeared_after_remediation,
                    factors_json, method_version, valuation_status,
                    monthly_gross, monthly_risk_adjusted, valuation_currency,
                    value_source, valuation_basis, cost_snapshot_id, cost_type,
                    cost_period_start, cost_period_end, valuation_method_version,
                    valuation_computed_at, current_monthly_cost,
                    target_monthly_cost, current_cost_basis,
                    target_price_basis, target_price_snapshot_id,
                    target_price_status, target_hourly_price,
                    target_hours_per_month, target_meter_id,
                    target_meter_name, target_product_name,
                    target_price_effective_start, operating_system,
                    license_model, actionability, actionability_reason
                FROM opportunity_actionable
                {where}
                ORDER BY {sort_expression} {sort_direction} NULLS LAST,
                    estimated_monthly_savings DESC NULLS LAST,
                    title
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            facets = {
                "resourceTypes": [
                    row[0]
                    for row in db.execute(
                        f"""
                        {base}
                        SELECT DISTINCT resource_type
                        FROM opportunity_actionable
                        WHERE resource_type <> ''
                        ORDER BY 1
                        """
                    ).fetchall()
                ],
                "subscriptions": [
                    {"id": row[0], "name": row[1] or row[0]}
                    for row in db.execute(
                        f"""
                        {base}
                        SELECT DISTINCT subscription_id, subscription_name
                        FROM opportunity_actionable
                        WHERE subscription_id <> ''
                        ORDER BY 2, 1
                        """
                    ).fetchall()
                ],
                "regions": [
                    row[0]
                    for row in db.execute(
                        f"""
                        {base}
                        SELECT DISTINCT region
                        FROM opportunity_actionable
                        WHERE region <> ''
                        ORDER BY 1
                        """
                    ).fetchall()
                ],
                "sources": [
                    row[0]
                    for row in db.execute(
                        f"""
                        {base}
                        SELECT DISTINCT source
                        FROM opportunity_actionable
                        ORDER BY 1
                        """
                    ).fetchall()
                ],
                "categories": [
                    row[0]
                    for row in db.execute(
                        f"{base} SELECT DISTINCT category FROM opportunity_actionable ORDER BY 1"
                    ).fetchall()
                ],
                "confidences": [
                    row[0]
                    for row in db.execute(
                        f"""
                        {base} SELECT DISTINCT confidence FROM opportunity_actionable
                        WHERE confidence <> '' ORDER BY 1
                        """
                    ).fetchall()
                ],
                "actionabilities": [
                    row[0]
                    for row in db.execute(
                        f"""
                        {base}
                        SELECT DISTINCT actionability
                        FROM opportunity_actionable
                        ORDER BY 1
                        """
                    ).fetchall()
                ],
            }
            summary_row = db.execute(
                f"""
                {base}
                SELECT
                    count(*),
                    count(*) FILTER (WHERE lower(impact) = 'high'),
                    count(*) FILTER (
                        WHERE source_set LIKE '%flux_intelligence%'
                          AND lower(confidence) = 'high'
                    ),
                    COALESCE(sum(estimated_monthly_savings), 0),
                    COALESCE(sum(annual_savings_amount), 0),
                    count(*) FILTER (WHERE source_count > 1),
                    count(DISTINCT resource_id) FILTER (
                        WHERE resource_id <> ''
                          AND resource_id <> concat(
                              '/subscriptions/',
                              lower(subscription_id)
                          )
                    ),
                    count(*) FILTER (
                        WHERE resource_id = concat(
                            '/subscriptions/',
                            lower(subscription_id)
                        )
                    )
                FROM opportunity_actionable
                {where}
                """,
                params,
            ).fetchone()
            portfolio_row = db.execute(
                f"""
                {base}
                SELECT
                    count(*) FILTER (
                        WHERE actionability <> 'governance_review'
                    ),
                    count(*) FILTER (
                        WHERE actionability = 'actionable_now'
                    ),
                    count(*) FILTER (
                        WHERE actionability = 'portfolio_review'
                    ),
                    count(*) FILTER (
                        WHERE actionability = 'evidence_needed'
                    ),
                    count(*) FILTER (
                        WHERE actionability = 'governance_review'
                    ),
                    count(*) FILTER (
                        WHERE monthly_risk_adjusted IS NOT NULL
                    ),
                    count(*) FILTER (WHERE source_count > 1),
                    count(*) FILTER (
                        WHERE json_extract_string(
                            factors_json, '$.telemetryStatus'
                        ) = 'covered'
                    )
                FROM opportunity_actionable
                """
            ).fetchone()
            actionable_by_source = db.execute(
                f"""
                {base}
                SELECT source, count(*)
                FROM opportunity_actionable
                WHERE actionability = 'actionable_now'
                GROUP BY source
                ORDER BY count(*) DESC, source
                """
            ).fetchall()
            source_diagnostics = db.execute(
                """
                SELECT 'azure_advisor', count(*),
                       count(DISTINCT recommendation_id)
                FROM advisor_recommendation_snapshots
                WHERE snapshot_id = (
                    SELECT arg_max(snapshot_id, observed_at)
                    FROM source_sync_state
                    WHERE source = 'AzureAdvisor'
                      AND scope_id = 'configured-subscriptions'
                )
                UNION ALL
                SELECT 'flux_intelligence', count(*),
                       count(DISTINCT finding_id)
                FROM rule_opportunity_snapshots
                WHERE snapshot_id = (
                    SELECT arg_max(snapshot_id, observed_at)
                    FROM source_sync_state
                    WHERE source = 'FluxIntelligence'
                      AND scope_id = 'configured-subscriptions'
                )
                ORDER BY 1
                """
            ).fetchall()
            exposure_row = db.execute(
                f"""
                {base}
                SELECT COALESCE(sum(actual_cost), 0)
                FROM (
                    SELECT resource_id, max(actual_cost) AS actual_cost
                    FROM opportunity_actionable
                    {where}
                    GROUP BY resource_id
                )
                """,
                params,
            ).fetchone()
            valuation_row = db.execute(
                f"""
                {base}
                SELECT
                    count(*) FILTER (
                        WHERE monthly_gross IS NOT NULL
                    ),
                    COALESCE(sum(monthly_gross), 0),
                    COALESCE(sum(monthly_risk_adjusted), 0),
                    count(*) FILTER (
                        WHERE value_source LIKE '%_minus_retail_target'
                    ),
                    COALESCE(sum(current_monthly_cost) FILTER (
                        WHERE value_source LIKE '%_minus_retail_target'
                    ), 0),
                    COALESCE(sum(target_monthly_cost) FILTER (
                        WHERE value_source LIKE '%_minus_retail_target'
                    ), 0)
                FROM (
                    SELECT
                        resource_id,
                        max(monthly_gross) AS monthly_gross,
                        max(monthly_risk_adjusted) AS monthly_risk_adjusted,
                        arg_max(value_source, monthly_gross) AS value_source,
                        arg_max(
                            current_monthly_cost,
                            monthly_gross
                        ) AS current_monthly_cost,
                        arg_max(
                            target_monthly_cost,
                            monthly_gross
                        ) AS target_monthly_cost
                    FROM opportunity_actionable
                    {where}
                    GROUP BY resource_id
                )
                """,
                params,
            ).fetchone()
        lifecycles = self.opportunity_lifecycles()
        # Configured labels as the fallback: findings on subscriptions with
        # no inventory rows yet (freshly added scopes) otherwise render a
        # raw GUID everywhere the finding appears.
        labels = self.subscription_labels()
        return {
            "items": [
                {
                    "id": row[0],
                    "lifecycleStatus": lifecycles.get(str(row[0]), "open"),
                    "source": row[1],
                    "kind": row[2],
                    "category": row[3],
                    "impact": row[4],
                    "confidence": row[5],
                    "title": row[6],
                    "reason": row[7],
                    "resourceId": row[8],
                    "relatedResourceId": row[9],
                    "resourceName": row[10],
                    "resourceType": row[11],
                    "subscriptionId": row[12],
                    "subscriptionName": (
                        row[13]
                        if row[13] and row[13] != row[12]
                        else labels.get(str(row[12] or "").lower())
                        or row[13]
                        or row[12]
                    ),
                    "resourceGroup": row[14],
                    "region": row[15],
                    "estimatedMonthlySavings": row[16],
                    "annualSavingsAmount": row[17],
                    "savingsCurrency": row[18],
                    "actualMonthlyCost": row[19],
                    "currentSku": row[20],
                    "recommendedSku": row[21],
                    "lastUpdated": row[22].isoformat() if row[22] else None,
                    "learnMoreLink": row[23],
                    "observedAt": row[24].isoformat(),
                    "corroboratedSources": row[26].split(","),
                    "isCorroborated": row[25] > 1,
                    "family": row[27],
                    "confidenceScore": row[28],
                    "firstSeen": row[29].isoformat() if row[29] else None,
                    "lastSeen": row[30].isoformat() if row[30] else None,
                    "ageDays": row[31],
                    "consecutiveCount": row[32],
                    "reappearedAfterRemediation": row[33] or False,
                    "confidenceFactors": json.loads(row[34]) if row[34] else None,
                    "confidenceMethodVersion": row[35] or "",
                    "valuationStatus": row[36] or "not_valued",
                    "monthlyGrossSavings": row[37],
                    "monthlyRiskAdjustedSavings": row[38],
                    "valuationCurrency": row[39] or row[18],
                    "valuationSource": row[40] or "",
                    "valuationBasis": row[41] or "",
                    "valuationCostSnapshotId": row[42] or "",
                    "valuationCostType": row[43] or "",
                    "valuationPeriodStart": row[44].isoformat() if row[44] else None,
                    "valuationPeriodEnd": row[45].isoformat() if row[45] else None,
                    "valuationMethodVersion": row[46] or "",
                    "valuationComputedAt": row[47].isoformat() if row[47] else None,
                    "currentMonthlyCostRunRate": row[48],
                    "targetMonthlyRetailCost": row[49],
                    "currentCostBasis": row[50] or "",
                    "targetPriceBasis": row[51] or "",
                    "targetPriceSnapshotId": row[52] or "",
                    "targetPriceStatus": row[53] or "",
                    "targetHourlyPrice": row[54],
                    "targetHoursPerMonth": row[55],
                    "targetMeterId": row[56] or "",
                    "targetMeterName": row[57] or "",
                    "targetProductName": row[58] or "",
                    "targetPriceEffectiveStart": (
                        row[59].isoformat() if row[59] else None
                    ),
                    "priceOperatingSystem": row[60] or "",
                    "priceLicenseModel": row[61] or "",
                    "actionability": row[62],
                    "actionabilityReason": row[63],
                    # Compatibility-preserving hard-gate view: actionability
                    # still serves existing filters, while these fields make
                    # clear that prioritization is not execution permission.
                    "recommendationStatus": (
                        "dismissed" if lifecycles.get(str(row[0])) == "dismissed"
                        else "candidate" if row[62] == "actionable_now"
                        else "evidence_needed"
                    ),
                    "executionStatus": {
                        "accepted": "prechecks_needed",
                        "implemented": "executed",
                        "dismissed": "suppressed",
                    }.get(lifecycles.get(str(row[0]), "open"), "not_requested"),
                    "executionBlockers": (
                        [] if lifecycles.get(str(row[0])) == "implemented" else [
                            "owner_approval",
                            "prechecks",
                            "final_resource_revalidation",
                        ]
                    ),
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "facets": facets,
            "summary": {
                "total": summary_row[0],
                "highImpact": summary_row[1],
                "highConfidenceFlux": summary_row[2],
                "estimatedMonthlySavings": summary_row[3],
                "annualSavings": summary_row[4],
                "corroborated": summary_row[5],
                "distinctResources": summary_row[6],
                "subscriptionScoped": summary_row[7],
                "costExposure": exposure_row[0],
                "valuedCount": valuation_row[0],
                "monthlyGrossValue": valuation_row[1],
                "monthlyRiskAdjustedValue": valuation_row[2],
                "skuValuedCount": valuation_row[3],
                "skuCurrentMonthlyCost": valuation_row[4],
                "skuTargetMonthlyCost": valuation_row[5],
                "portfolio": {
                    "detected": portfolio_row[0],
                    "actionableNow": portfolio_row[1],
                    "portfolioReview": portfolio_row[2],
                    "evidenceNeeded": portfolio_row[3],
                    "governanceReview": portfolio_row[4],
                    "valued": portfolio_row[5],
                    "corroborated": portfolio_row[6],
                    "telemetryReady": portfolio_row[7],
                    "actionableBySource": [
                        {"source": row[0], "count": row[1]}
                        for row in actionable_by_source
                    ],
                },
            },
            "diagnostics": {
                "sourceRows": [
                    {
                        "source": row[0],
                        "rawRows": row[1],
                        "uniqueIds": row[2],
                        "duplicates": row[1] - row[2],
                    }
                    for row in source_diagnostics
                ],
                "visibleActions": summary_row[0],
                "distinctResources": summary_row[6],
                "subscriptionScoped": summary_row[7],
            },
        }

    def _persist_intelligence_usage(
        self,
        db: Any,
        *,
        usage_values: list[Any],
        transcript_values: list[Any],
        retention_days: int,
        transcript_retention_days: int,
    ) -> None:
        db.execute(
            """
            DELETE FROM intelligence_usage_events
            WHERE occurred_at < current_timestamp - (? * INTERVAL '1 day')
            """,
            [max(1, retention_days)],
        )
        if transcript_retention_days > 0:
            db.execute(
                """
                DELETE FROM intelligence_transcript_events
                WHERE occurred_at < current_timestamp - (? * INTERVAL '1 day')
                """,
                [transcript_retention_days],
            )
        db.execute(
            """
            INSERT INTO intelligence_usage_events (
                request_id, occurred_at, user_hash, provider, model,
                status, latency_ms, prompt_tokens, cached_prompt_tokens,
                completion_tokens, estimated_cost_usd, tool_names_json,
                tool_call_count, error_code, model_latency_ms,
                governed_tool_latency_ms, database_latency_ms,
                validation_latency_ms, application_latency_ms,
                model_call_count, tool_latency_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (request_id) DO UPDATE SET
                occurred_at = excluded.occurred_at,
                user_hash = excluded.user_hash,
                provider = excluded.provider,
                model = excluded.model,
                status = excluded.status,
                latency_ms = excluded.latency_ms,
                prompt_tokens = excluded.prompt_tokens,
                cached_prompt_tokens = excluded.cached_prompt_tokens,
                completion_tokens = excluded.completion_tokens,
                estimated_cost_usd = excluded.estimated_cost_usd,
                tool_names_json = excluded.tool_names_json,
                tool_call_count = excluded.tool_call_count,
                error_code = excluded.error_code,
                model_latency_ms = excluded.model_latency_ms,
                governed_tool_latency_ms = excluded.governed_tool_latency_ms,
                database_latency_ms = excluded.database_latency_ms,
                validation_latency_ms = excluded.validation_latency_ms,
                application_latency_ms = excluded.application_latency_ms,
                model_call_count = excluded.model_call_count,
                tool_latency_json = excluded.tool_latency_json
            """,
            usage_values,
        )
        if transcript_retention_days > 0:
            db.execute(
                """
                INSERT INTO intelligence_transcript_events (
                    request_id, occurred_at, user_hash, messages_json,
                    context_json, response_json, raw_response_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (request_id) DO UPDATE SET
                    occurred_at = excluded.occurred_at,
                    user_hash = excluded.user_hash,
                    messages_json = excluded.messages_json,
                    context_json = excluded.context_json,
                    response_json = excluded.response_json,
                    raw_response_text = excluded.raw_response_text
                """,
                transcript_values,
            )

    def record_intelligence_usage(
        self,
        *,
        request_id: str,
        user_hash: str,
        provider: str,
        model: str,
        status: str,
        latency_ms: int,
        prompt_tokens: int,
        cached_prompt_tokens: int,
        completion_tokens: int,
        estimated_cost_usd: float,
        tool_names: list[str],
        error_code: str = "",
        retention_days: int = 30,
        model_latency_ms: int = 0,
        governed_tool_latency_ms: int = 0,
        database_latency_ms: int = 0,
        validation_latency_ms: int = 0,
        application_latency_ms: int = 0,
        model_call_count: int = 0,
        tool_latency: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
        response: dict[str, Any] | None = None,
        raw_response_text: str = "",
        transcript_retention_days: int = 0,
    ) -> None:
        observed_at = utc_now()
        usage_values = [
            request_id,
            observed_at,
            user_hash,
            provider,
            model,
            status,
            max(0, latency_ms),
            max(0, prompt_tokens),
            max(0, cached_prompt_tokens),
            max(0, completion_tokens),
            max(0.0, estimated_cost_usd),
            json_value(sorted(set(tool_names))),
            len(tool_names),
            error_code[:120],
            max(0, model_latency_ms),
            max(0, governed_tool_latency_ms),
            max(0, database_latency_ms),
            max(0, validation_latency_ms),
            max(0, application_latency_ms),
            max(0, model_call_count),
            json_value(tool_latency or []),
        ]
        transcript_values = [
            request_id,
            observed_at,
            user_hash,
            json_value(messages or []),
            json_value(context or {}),
            json_value(response) if response is not None else None,
            raw_response_text[:50000],
        ]
        with self.operational_connect() as db:
            self._persist_intelligence_usage(
                db,
                usage_values=usage_values,
                transcript_values=transcript_values,
                retention_days=retention_days,
                transcript_retention_days=transcript_retention_days,
            )
        return

        with self.operational_connect() as db:
            db.execute(
                """
                DELETE FROM intelligence_usage_events
                WHERE occurred_at < current_timestamp - (? * INTERVAL '1 day')
                """,
                [max(1, retention_days)],
            )
            if transcript_retention_days > 0:
                db.execute(
                    """
                    DELETE FROM intelligence_transcript_events
                    WHERE occurred_at < current_timestamp - (? * INTERVAL '1 day')
                    """,
                    [transcript_retention_days],
                )
            db.execute(
                """
                INSERT INTO intelligence_usage_events (
                    request_id, occurred_at, user_hash, provider, model,
                    status, latency_ms, prompt_tokens, cached_prompt_tokens,
                    completion_tokens, estimated_cost_usd, tool_names_json,
                    tool_call_count, error_code, model_latency_ms,
                    governed_tool_latency_ms, database_latency_ms,
                    validation_latency_ms, application_latency_ms,
                    model_call_count, tool_latency_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    request_id,
                    utc_now(),
                    user_hash,
                    provider,
                    model,
                    status,
                    max(0, latency_ms),
                    max(0, prompt_tokens),
                    max(0, cached_prompt_tokens),
                    max(0, completion_tokens),
                    max(0.0, estimated_cost_usd),
                    json_value(sorted(set(tool_names))),
                    len(tool_names),
                    error_code[:120],
                    max(0, model_latency_ms),
                    max(0, governed_tool_latency_ms),
                    max(0, database_latency_ms),
                    max(0, validation_latency_ms),
                    max(0, application_latency_ms),
                    max(0, model_call_count),
                    json_value(tool_latency or []),
                ],
            )
            if transcript_retention_days > 0:
                db.execute(
                    """
                    INSERT INTO intelligence_transcript_events (
                        request_id, occurred_at, user_hash, messages_json,
                        context_json, response_json, raw_response_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (request_id) DO UPDATE SET
                        occurred_at = excluded.occurred_at,
                        user_hash = excluded.user_hash,
                        messages_json = excluded.messages_json,
                        context_json = excluded.context_json,
                        response_json = excluded.response_json,
                        raw_response_text = excluded.raw_response_text
                    """,
                    [
                        request_id,
                        utc_now(),
                        user_hash,
                        json_value(messages or []),
                        json_value(context or {}),
                        json_value(response) if response is not None else None,
                        raw_response_text[:50000],
                    ],
                )
        with self.connect() as db:
            db.execute(
                """
                DELETE FROM intelligence_usage_events
                WHERE occurred_at < current_timestamp - (? * INTERVAL '1 day')
                """,
                [max(1, retention_days)],
            )
            if transcript_retention_days > 0:
                db.execute(
                    """
                    DELETE FROM intelligence_transcript_events
                    WHERE occurred_at < current_timestamp - (? * INTERVAL '1 day')
                    """,
                    [transcript_retention_days],
                )
            db.execute(
                """
                INSERT INTO intelligence_usage_events (
                    request_id, occurred_at, user_hash, provider, model,
                    status, latency_ms, prompt_tokens, cached_prompt_tokens,
                    completion_tokens, estimated_cost_usd, tool_names_json,
                    tool_call_count, error_code, model_latency_ms,
                    governed_tool_latency_ms, database_latency_ms,
                    validation_latency_ms, application_latency_ms,
                    model_call_count, tool_latency_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (request_id) DO UPDATE SET
                    occurred_at = excluded.occurred_at,
                    user_hash = excluded.user_hash,
                    provider = excluded.provider,
                    model = excluded.model,
                    status = excluded.status,
                    latency_ms = excluded.latency_ms,
                    prompt_tokens = excluded.prompt_tokens,
                    cached_prompt_tokens = excluded.cached_prompt_tokens,
                    completion_tokens = excluded.completion_tokens,
                    estimated_cost_usd = excluded.estimated_cost_usd,
                    tool_names_json = excluded.tool_names_json,
                    tool_call_count = excluded.tool_call_count,
                    error_code = excluded.error_code,
                    model_latency_ms = excluded.model_latency_ms,
                    governed_tool_latency_ms = excluded.governed_tool_latency_ms,
                    database_latency_ms = excluded.database_latency_ms,
                    validation_latency_ms = excluded.validation_latency_ms,
                    application_latency_ms = excluded.application_latency_ms,
                    model_call_count = excluded.model_call_count,
                    tool_latency_json = excluded.tool_latency_json
                ON CONFLICT (request_id) DO UPDATE SET
                    occurred_at = excluded.occurred_at,
                    user_hash = excluded.user_hash,
                    provider = excluded.provider,
                    model = excluded.model,
                    status = excluded.status,
                    latency_ms = excluded.latency_ms,
                    prompt_tokens = excluded.prompt_tokens,
                    cached_prompt_tokens = excluded.cached_prompt_tokens,
                    completion_tokens = excluded.completion_tokens,
                    estimated_cost_usd = excluded.estimated_cost_usd,
                    tool_names_json = excluded.tool_names_json,
                    tool_call_count = excluded.tool_call_count,
                    error_code = excluded.error_code,
                    model_latency_ms = excluded.model_latency_ms,
                    governed_tool_latency_ms = excluded.governed_tool_latency_ms,
                    database_latency_ms = excluded.database_latency_ms,
                    validation_latency_ms = excluded.validation_latency_ms,
                    application_latency_ms = excluded.application_latency_ms,
                    model_call_count = excluded.model_call_count,
                    tool_latency_json = excluded.tool_latency_json
                """,
                [
                    request_id,
                    utc_now(),
                    user_hash,
                    provider,
                    model,
                    status,
                    max(0, latency_ms),
                    max(0, prompt_tokens),
                    max(0, cached_prompt_tokens),
                    max(0, completion_tokens),
                    max(0.0, estimated_cost_usd),
                    json_value(sorted(set(tool_names))),
                    len(tool_names),
                    error_code[:120],
                    max(0, model_latency_ms),
                    max(0, governed_tool_latency_ms),
                    max(0, database_latency_ms),
                    max(0, validation_latency_ms),
                    max(0, application_latency_ms),
                    max(0, model_call_count),
                    json_value(tool_latency or []),
                ],
            )
            if transcript_retention_days > 0:
                db.execute(
                    """
                    INSERT INTO intelligence_transcript_events (
                        request_id, occurred_at, user_hash, messages_json,
                        context_json, response_json, raw_response_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (request_id) DO UPDATE SET
                        occurred_at = excluded.occurred_at,
                        user_hash = excluded.user_hash,
                        messages_json = excluded.messages_json,
                        context_json = excluded.context_json,
                        response_json = excluded.response_json,
                        raw_response_text = excluded.raw_response_text
                    ON CONFLICT (request_id) DO UPDATE SET
                        occurred_at = excluded.occurred_at,
                        user_hash = excluded.user_hash,
                        messages_json = excluded.messages_json,
                        context_json = excluded.context_json,
                        response_json = excluded.response_json,
                        raw_response_text = excluded.raw_response_text
                    """,
                    [
                        request_id,
                        utc_now(),
                        user_hash,
                        json_value(messages or []),
                        json_value(context or {}),
                        json_value(response) if response is not None else None,
                        raw_response_text[:50000],
                    ],
                )

    def record_intelligence_client_performance(
        self,
        *,
        request_id: str,
        user_hash: str,
        client_round_trip_ms: int,
        client_render_ms: int,
        client_end_to_end_ms: int,
    ) -> bool:
        with self.operational_connect() as db:
            result = db.execute(
                """
                UPDATE intelligence_usage_events
                SET client_round_trip_ms = ?,
                    client_render_ms = ?,
                    client_end_to_end_ms = ?,
                    transport_ingress_ms = greatest(0, ? - latency_ms)
                WHERE request_id = ? AND user_hash = ?
                RETURNING request_id
                """,
                [
                    max(0, client_round_trip_ms),
                    max(0, client_render_ms),
                    max(0, client_end_to_end_ms),
                    max(0, client_round_trip_ms),
                    request_id,
                    user_hash,
                ],
            ).fetchone()
        return bool(result)

    def intelligence_transcript_review(
        self,
        limit: int = 50,
        retention_days: int = 30,
    ) -> dict[str, Any]:
        with self.operational_connect(read_only=True) as db:
            rows = db.execute(
                """
                SELECT transcript.request_id, transcript.occurred_at,
                       left(transcript.user_hash, 12), usage.status,
                       usage.latency_ms, usage.client_end_to_end_ms,
                       usage.model_latency_ms, usage.governed_tool_latency_ms,
                       usage.database_latency_ms, usage.validation_latency_ms,
                       usage.application_latency_ms, usage.transport_ingress_ms,
                       usage.client_render_ms, usage.tool_latency_json,
                       transcript.messages_json, transcript.context_json,
                       transcript.response_json, transcript.raw_response_text,
                       usage.feedback_rating
                FROM intelligence_transcript_events AS transcript
                LEFT JOIN intelligence_usage_events AS usage USING (request_id)
                ORDER BY transcript.occurred_at DESC
                LIMIT ?
                """,
                [max(1, min(limit, 100))],
            ).fetchall()
        return {
            "retentionDays": max(0, retention_days),
            "items": [
                {
                    "requestId": row[0],
                    "occurredAt": row[1].isoformat() if row[1] else None,
                    "userHashPrefix": row[2],
                    "status": row[3],
                    "performance": {
                        "serverMs": row[4] or 0,
                        "clientEndToEndMs": row[5],
                        "modelMs": row[6] or 0,
                        "governedToolMs": row[7] or 0,
                        "databaseMs": row[8] or 0,
                        "validationMs": row[9] or 0,
                        "applicationMs": row[10] or 0,
                        "transportAndIngressMs": row[11],
                        "renderMs": row[12],
                        "toolDurations": json.loads(row[13] or "[]"),
                        "rillInPath": False,
                    },
                    "messages": json.loads(row[14] or "[]"),
                    "context": json.loads(row[15] or "{}"),
                    "response": json.loads(row[16]) if row[16] else None,
                    "rawResponse": row[17],
                    "feedback": row[18],
                }
                for row in rows
            ],
        }

    def intelligence_usage_status(self, retention_days: int = 30) -> dict[str, Any]:
        latency_p95 = (
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)"
            if self.operational_backend == "postgres"
            else "quantile_cont(latency_ms, 0.95)"
        )
        client_p95 = (
            "percentile_cont(0.95) WITHIN GROUP "
            "(ORDER BY client_end_to_end_ms)"
            if self.operational_backend == "postgres"
            else "quantile_cont(client_end_to_end_ms, 0.95)"
        )
        with self.operational_connect(read_only=True) as db:
            row = db.execute(
                f"""
                SELECT count(*), COALESCE(sum(estimated_cost_usd), 0),
                       COALESCE(sum(prompt_tokens), 0),
                       COALESCE(sum(completion_tokens), 0),
                       max(occurred_at),
                       COALESCE(avg(latency_ms), 0),
                       COALESCE({latency_p95}, 0),
                       COALESCE(sum(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END), 0),
                       COALESCE(sum(CASE WHEN status <> 'succeeded' THEN 1 ELSE 0 END), 0),
                       COALESCE(avg(client_end_to_end_ms), 0),
                       COALESCE({client_p95}, 0),
                       COALESCE(avg(model_latency_ms), 0),
                       COALESCE(avg(governed_tool_latency_ms), 0),
                       COALESCE(avg(database_latency_ms), 0),
                       COALESCE(avg(transport_ingress_ms), 0),
                       COALESCE(avg(client_render_ms), 0)
                FROM intelligence_usage_events
                WHERE occurred_at >= current_timestamp - (? * INTERVAL '1 day')
                """,
                [max(1, retention_days)],
            ).fetchone()
        return {
            "requestCount": row[0],
            "estimatedCostUsd": round(row[1], 6),
            "promptTokens": row[2],
            "completionTokens": row[3],
            "lastRequestAt": row[4].isoformat() if row[4] else None,
            "averageLatencyMs": round(float(row[5] or 0)),
            "p95LatencyMs": round(float(row[6] or 0)),
            "successfulRequestCount": int(row[7] or 0),
            "failedRequestCount": int(row[8] or 0),
            "averageClientEndToEndMs": round(float(row[9] or 0)),
            "p95ClientEndToEndMs": round(float(row[10] or 0)),
            "averageModelLatencyMs": round(float(row[11] or 0)),
            "averageGovernedToolLatencyMs": round(float(row[12] or 0)),
            "averageDatabaseLatencyMs": round(float(row[13] or 0)),
            "averageTransportIngressMs": round(float(row[14] or 0)),
            "averageRenderMs": round(float(row[15] or 0)),
            "retentionDays": max(1, retention_days),
        }

    def record_intelligence_feedback(
        self,
        request_id: str,
        rating: str,
        reason: str,
    ) -> bool:
        with self.operational_connect() as db:
            result = db.execute(
                """
                UPDATE intelligence_usage_events
                SET feedback_rating = ?, feedback_reason = ?, feedback_at = ?
                WHERE request_id = ?
                RETURNING request_id
                """,
                [rating, reason[:500], utc_now(), request_id],
            ).fetchone()
        return bool(result)

    def seed_demo(self) -> None:
        with self.connect(read_only=True) as db:
            if db.execute("SELECT COUNT(*) FROM resource_snapshots").fetchone()[0]:
                return
        snapshot_id = f"demo-{uuid4()}"
        demo = [
            ("checkout-api", "microsoft.compute/virtualmachines", "eastus2", "Standard_D4s_v5", 422.30, 62.0, None),
            ("batch-worker-01", "microsoft.compute/virtualmachines", "eastus2", "Standard_D8s_v5", 731.10, 8.0, "low_utilization"),
            ("orders-db", "microsoft.sql/servers/databases", "eastus2", "GeneralPurpose", 980.00, 71.0, None),
            ("logs-prod", "microsoft.operationalinsights/workspaces", "eastus2", "PerGB2018", 514.45, None, None),
            ("orphan-disk-17", "microsoft.compute/disks", "westus3", "Premium_LRS", 135.17, None, "unattached_disk"),
            ("edge-ip-old", "microsoft.network/publicipaddresses", "westus3", "Standard", 4.00, None, "unattached_public_ip"),
            ("catalog-api", "microsoft.app/containerapps", "centralus", "Consumption", 188.90, 44.0, None),
            ("archive-data", "microsoft.storage/storageaccounts", "centralus", "Standard_GRS", 276.25, None, None),
        ]
        resources = []
        for index, item in enumerate(demo):
            name, resource_type, region, sku, cost, utilization, opportunity = item
            resources.append(
                {
                    "resourceId": f"/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/flux-demo/providers/{resource_type}/{name}",
                    "name": name,
                    "resourceType": resource_type,
                    "subscriptionId": "00000000-0000-0000-0000-000000000001",
                    "subscriptionName": "Platform Production",
                    "resourceGroup": "flux-demo",
                    "region": region,
                    "kind": "",
                    "sku": sku,
                    "provisioningState": "Succeeded",
                    "managedBy": "" if opportunity else "platform",
                    "tags": {"environment": "production", "owner": "platform"} if index != 5 else {},
                    "estimatedMonthlyCost": cost,
                    "costSource": "demo",
                    "utilizationPercent": utilization,
                    "utilizationSource": "demo" if utilization is not None else None,
                    "opportunityKind": opportunity,
                    "opportunityReason": {
                        "low_utilization": "Average utilization is below 10%; validate sizing and schedule.",
                        "unattached_disk": "Managed disk has no owning resource.",
                        "unattached_public_ip": "Public IP has no owning resource.",
                    }.get(opportunity),
                    "estimatedMonthlySavings": cost * 0.5 if opportunity else None,
                    "raw": {"demo": True},
                }
            )
        self.store_snapshot(snapshot_id, resources)
        # Fourteen months of deterministic monthly totals so the fiscal-year
        # outlook has seasonal history in dev and tests. The current month is
        # seeded as a partial total, the way Cost Management reports it.
        demo_subscription = "00000000-0000-0000-0000-000000000001"
        today = date.today()
        current_month = today.replace(day=1)
        for cost_type, scale, offset_amount in (
            ("AmortizedCost", 1.0, 0.0),
            ("ActualCost", 0.98, 40.0),
        ):
            monthly_records = []
            for back in range(13, -1, -1):
                total_index = (
                    current_month.year * 12 + current_month.month - 1 - back
                )
                month = date(total_index // 12, total_index % 12 + 1, 1)
                growth = 1.012 ** (13 - back)
                seasonal = 1 + 0.06 * (((month.month * 7) % 5) - 2) / 2
                amount = 3200.0 * growth * seasonal * scale + offset_amount
                if month == current_month:
                    amount *= max((today.day - 1), 1) / 30
                monthly_records.append(
                    {
                        "month": month.isoformat(),
                        "costType": cost_type,
                        "subscriptionId": demo_subscription,
                        "amount": round(amount, 2),
                        "currency": "USD",
                        "source": "demo",
                    }
                )
            self.store_monthly_cost_scope(
                f"{snapshot_id}:monthly:{cost_type}",
                demo_subscription,
                cost_type,
                monthly_records,
                start_month=date.fromisoformat(monthly_records[0]["month"]),
                end_month=current_month,
            )
        self.store_commitments(
            f"{snapshot_id}:commitments",
            [
                {
                    "reservationId": "demo-ri-d4", "orderId": "demo-order-1",
                    "displayName": "VM_RI_demo_d4", "sku": "Standard_D4s_v5",
                    "resourceType": "VirtualMachines", "region": "eastus2",
                    "quantity": 4, "term": "P3Y",
                    "scopeType": "Single subscription", "state": "Succeeded",
                    "expiryDate": (today + timedelta(days=95)).isoformat(),
                    "utilization1d": 100.0, "utilization7d": 99.5,
                    "utilization30d": 98.7,
                },
                {
                    "reservationId": "demo-ri-b2", "orderId": "demo-order-2",
                    "displayName": "VM_RI_demo_b2", "sku": "Standard_B2s",
                    "resourceType": "VirtualMachines", "region": "westus3",
                    "quantity": 2, "term": "P1Y",
                    "scopeType": "Shared", "state": "Succeeded",
                    "expiryDate": (today + timedelta(days=300)).isoformat(),
                    "utilization1d": 71.0, "utilization7d": 74.2,
                    "utilization30d": 76.4,
                },
                {
                    "reservationId": "demo-ri-old", "orderId": "demo-order-3",
                    "displayName": "VM_RI_demo_expired", "sku": "Standard_E4as_v5",
                    "resourceType": "VirtualMachines", "region": "westus3",
                    "quantity": 4, "term": "P1Y",
                    "scopeType": "Single subscription", "state": "Expired",
                    "expiryDate": (today - timedelta(days=200)).isoformat(),
                    "utilization1d": None, "utilization7d": None,
                    "utilization30d": None,
                },
            ],
            [],
        )
        now = utc_now()
        with self.operational_connect() as db:
            db.execute(
                """
                INSERT INTO sync_runs (
                    id, provider, started_at, completed_at, status,
                    resource_count, message, trigger, stage, stage_message
                ) VALUES (?, 'demo', ?, ?, 'succeeded', ?, 'Demo data loaded.',
                    'seed', 'complete', 'Demo data loaded.')
                """,
                [snapshot_id, now, now, len(resources)],
            )
