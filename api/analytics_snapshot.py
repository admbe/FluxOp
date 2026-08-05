"""Immutable analytical snapshot publication and consumption.

Implements the interim-architecture seam from
``docs/FLUXFINOPS-POSTGRES-DUCKDB-INTERIM-MIGRATION-PLAN.md`` sections 12-13:

- the singleton writer publishes checkpointed, validated, checksummed copies
  of the mutable analytical DuckDB database to snapshot storage and records an
  approved publication in the operational control plane;
- each API instance downloads the approved snapshot, verifies it, opens it
  read-only, and serves every analytical read from that local immutable copy
  without ever acquiring the cross-process writer lease.

Snapshot storage is a private Blob container in production and a local
directory in development; both store the same versioned immutable files.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Any

import duckdb

from .semantic_layer import create_semantic_views

_logger = logging.getLogger("flux.analytics_snapshot")

# Tables whose absence means the candidate is not a usable analytical store.
_CRITICAL_TABLES = (
    "resource_snapshots",
    "cost_snapshots",
    "daily_cost_history",
    "advisor_recommendation_snapshots",
    "rule_opportunity_snapshots",
)

# Materialized current tables the API reads on nearly every page. Every
# materialized table belongs here: on 2026-07-31 the two that were actually
# corrupt (opportunity_valuation_current, rule_opportunities_current) were
# absent from this list, so a snapshot that crashed /api/overview was
# approved and published.
_CRITICAL_CURRENT = (
    "resources_current",
    "costs_current",
    "commitment_costs_current",
    "advisor_recommendations_current",
    "rule_opportunities_current",
    "opportunity_confidence_current",
    "opportunity_valuation_current",
    "policy_posture_current",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SnapshotValidationError(RuntimeError):
    """A candidate snapshot failed a validation gate and was not approved."""


class LocalSnapshotStorage:
    """Versioned immutable snapshot files in a local directory (development)."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def upload(self, source: Path, name: str) -> None:
        target = self.directory / name
        staging = target.with_name(f".{target.name}.uploading")
        shutil.copy2(source, staging)
        staging.replace(target)

    def download(self, name: str, target: Path) -> None:
        shutil.copy2(self.directory / name, target)

    def delete(self, name: str) -> None:
        (self.directory / name).unlink(missing_ok=True)


class BlobSnapshotStorage:
    """Versioned immutable snapshot files in a private Blob container."""

    def __init__(
        self,
        account_url: str,
        container: str,
        managed_identity_client_id: str = "",
    ):
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        credential = DefaultAzureCredential(
            managed_identity_client_id=managed_identity_client_id or None
        )
        service = BlobServiceClient(account_url=account_url, credential=credential)
        self._container = service.get_container_client(container)
        try:
            self._container.create_container()
        except Exception as error:
            if getattr(error, "status_code", None) != 409:
                raise

    def upload(self, source: Path, name: str) -> None:
        with source.open("rb") as stream:
            self._container.upload_blob(name, stream, overwrite=False)

    def download(self, name: str, target: Path) -> None:
        staging = target.with_name(f".{target.name}.downloading")
        with staging.open("wb") as stream:
            self._container.download_blob(name).readinto(stream)
        staging.replace(target)

    def delete(self, name: str) -> None:
        try:
            self._container.delete_blob(name)
        except Exception as error:
            if getattr(error, "status_code", None) != 404:
                raise


def validate_candidate(path: Path) -> dict[str, int]:
    """Run the approval gates against a candidate file.

    Returns critical row counts on success; raises SnapshotValidationError on
    any gate failure. The candidate is opened independently and read-only so a
    truncated or corrupt copy can never be approved.
    """
    try:
        connection = duckdb.connect(str(path), read_only=True)
    except Exception as error:
        raise SnapshotValidationError(f"candidate does not open: {error}") from error
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
        missing = [name for name in _CRITICAL_TABLES if name not in tables]
        if missing:
            raise SnapshotValidationError(f"missing critical tables: {missing}")
        row_counts: dict[str, int] = {}
        scanned = _CRITICAL_TABLES + tuple(t for t in _CRITICAL_CURRENT if t in tables)
        for name in scanned:
            count = connection.execute(f"SELECT count(*) FROM {name}").fetchone()
            row_counts[name] = int(count[0]) if count else 0
        # Full column scan, not a LIMIT 1 smoke test. Storage corruption lives
        # in individual column segments: on 2026-07-31 a snapshot whose
        # opportunity_valuation_current had a torn RLE segment passed the old
        # LIMIT 1 check (early pages read fine) and then segfaulted the web on
        # a full aggregate. Hashing every column forces each segment to be
        # decompressed here, where a bad candidate can still be rejected.
        for name in scanned:
            columns = [
                str(row[0])
                for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = ?",
                    [name],
                ).fetchall()
            ]
            if not columns:
                continue
            projection = ", ".join(
                f'sum(hash("{column}"))' for column in columns
            )
            try:
                connection.execute(f"SELECT {projection} FROM {name}").fetchall()
            except Exception as error:
                raise SnapshotValidationError(
                    f"full scan of {name} failed: {error}"
                ) from error
        return row_counts
    except SnapshotValidationError:
        raise
    except Exception as error:
        raise SnapshotValidationError(f"candidate failed smoke queries: {error}") from error
    finally:
        connection.close()


def storage_from_settings(settings: Any) -> Any:
    """Blob storage when an account URL is configured, else a local directory."""
    if settings.analytics_snapshot_storage_account_url:
        return BlobSnapshotStorage(
            account_url=settings.analytics_snapshot_storage_account_url,
            container=settings.analytics_snapshot_container,
            managed_identity_client_id=settings.managed_identity_client_id,
        )
    return LocalSnapshotStorage(settings.analytics_snapshot_local_directory)


def _reject_catastrophic_regression(
    row_counts: dict[str, int], previous: dict[str, Any] | None
) -> None:
    """Refuse a candidate whose critical tables collapsed to empty.

    Validation proves a file is *readable*, not that it is *right*. An empty
    but well-formed database passes every structural gate, so a wiped or
    freshly-initialised working copy would be approved and would silently
    replace good data for every reader. A table going from populated to zero
    is never a legitimate publication.
    """
    if not previous:
        return
    try:
        before = json.loads(previous.get("rowCounts") or "{}")
    except (TypeError, ValueError):
        return
    if not isinstance(before, dict):
        return
    emptied = [
        name
        for name, count in row_counts.items()
        if count == 0 and int(before.get(name) or 0) > 0
    ]
    if emptied:
        raise SnapshotValidationError(
            "critical tables regressed to empty since version "
            f"{previous.get('version')}: {', '.join(sorted(emptied))}"
        )


class SnapshotPublisher:
    """Checkpoint, validate, and publish the mutable analytical database.

    Owned by the singleton writer process. Publication is atomic from the
    consumer's point of view: the file is fully uploaded and checksummed
    before the approved publication row becomes visible.
    """

    def __init__(
        self,
        database: Any,
        storage: Any,
        retention: int = 5,
        min_interval_seconds: float = 0,
        daily_retention_days: int = 14,
    ):
        self.database = database
        self.storage = storage
        self.retention = max(1, retention)
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self.daily_retention_days = max(0, daily_retention_days)

    def publish(self, force: bool = False) -> dict[str, Any] | None:
        """Publish unless a fresh approved version already exists.

        Bursts of completed jobs coalesce: when the newest approved
        publication is younger than ``min_interval_seconds`` the publication
        is skipped (returned with status ``coalesced``) instead of producing
        another full copy that would be superseded within minutes. ``force``
        bypasses coalescing for operator-driven publications.
        """
        if not force and self.min_interval_seconds > 0:
            latest = self.database.latest_analytics_publication()
            if latest:
                from .database import parse_iso_timestamp, utc_now

                generated = parse_iso_timestamp(latest.get("generatedAt"))
                if generated is not None:
                    age = (utc_now() - generated).total_seconds()
                    if age < self.min_interval_seconds:
                        print(
                            f"Snapshot publication coalesced: version "
                            f"{latest['version']} is {age:.0f}s old "
                            f"(minimum interval {self.min_interval_seconds:.0f}s)."
                        )
                        return {**latest, "status": "coalesced"}
        import time as _time

        publish_started = _time.monotonic()
        with tempfile.TemporaryDirectory(prefix="flux-snapshot-") as directory:
            candidate = Path(directory) / "candidate.duckdb"
            # Hold the writer lease across checkpoint and copy so the file is
            # consistent, but close the DuckDB connection before copying —
            # Windows forbids copying a file another handle holds open.
            with self.database.writer_lease(timeout=-1):
                with self.database.connect() as connection:
                    connection.execute("CHECKPOINT")
                shutil.copy2(self.database.path, candidate)
            # The candidate is private, so the semantic views can be
            # (re)created on it without the writer lease. Doing it here
            # guarantees every published snapshot carries the registry's
            # current definitions even when the mutable file predates them.
            views_connection = duckdb.connect(str(candidate))
            try:
                create_semantic_views(views_connection)
            finally:
                views_connection.close()
            try:
                row_counts = validate_candidate(candidate)
                _reject_catastrophic_regression(
                    row_counts, self.database.latest_analytics_publication()
                )
            except SnapshotValidationError as error:
                _logger.error("Snapshot candidate rejected: %s", error)
                self.database.record_analytics_publication(
                    status="rejected",
                    file_name="",
                    checksum="",
                    file_size_bytes=0,
                    row_counts={},
                    message=str(error),
                )
                return None
            checksum = _sha256(candidate)
            size = candidate.stat().st_size
            version = self.database.next_analytics_publication_version()
            name = f"flux-analytics-{version:08d}.duckdb"
            self.storage.upload(candidate, name)
            duration_ms = int((_time.monotonic() - publish_started) * 1000)
            publication = self.database.record_analytics_publication(
                status="approved",
                file_name=name,
                checksum=checksum,
                file_size_bytes=size,
                row_counts=row_counts,
                message="",
                version=version,
                duration_ms=duration_ms,
            )
            # print in addition to logger: production suppresses INFO logs,
            # and publication success must be visible in the WebJob log.
            print(
                f"Published analytical snapshot {name} "
                f"(version {version}, {size:,} bytes)."
            )
            self._prune()
            return publication

    def _prune(self) -> None:
        stale = self.database.prune_analytics_publications(
            self.retention,
            daily_retention_days=self.daily_retention_days,
        )
        for name in stale:
            try:
                self.storage.delete(name)
            except Exception as error:
                _logger.warning("Could not delete stale snapshot %s: %s", name, error)


class AnalyticsSnapshotManager:
    """Keep a verified local copy of the approved snapshot for API reads.

    The active local file is immutable; refresh downloads a newer approved
    version to a new name, verifies its checksum, smoke-opens it, and then
    atomically swaps the database's read path. In-flight requests keep their
    already-open connections to the previous file.
    """

    def __init__(self, database: Any, storage: Any, local_directory: Path):
        self.database = database
        self.storage = storage
        self.local_directory = Path(local_directory)
        self.local_directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._active_version: int | None = None

    @property
    def active_version(self) -> int | None:
        return self._active_version

    def refresh_once(self) -> bool:
        """Adopt the newest approved publication. Returns True on a swap."""
        publication = self.database.latest_analytics_publication()
        if not publication:
            return False
        version = int(publication["version"])
        with self._lock:
            if self._active_version is not None and version <= self._active_version:
                return False
            name = publication["fileName"]
            local = self.local_directory / name
            if not local.exists():
                try:
                    self.storage.download(name, local)
                except Exception as error:
                    _logger.error("Snapshot download failed for %s: %s", name, error)
                    return False
            checksum = _sha256(local)
            if checksum != publication["checksum"]:
                _logger.error(
                    "Snapshot checksum mismatch for %s; discarding local copy.", name
                )
                local.unlink(missing_ok=True)
                return False
            try:
                validate_candidate(local)
            except SnapshotValidationError as error:
                _logger.error("Downloaded snapshot failed validation: %s", error)
                return False
            self.database.attach_read_snapshot(local)
            previous = self._active_version
            self._active_version = version
            # print in addition to logger: production suppresses INFO logs,
            # and adoption must be visible in the container log.
            print(
                f"Serving analytical reads from snapshot version {version}"
                + ("" if previous is None else f" (previously {previous})")
                + "."
            )
            self._prune_local(keep=name)
            return True

    def _prune_local(self, keep: str) -> None:
        snapshots = sorted(
            self.local_directory.glob("flux-analytics-*.duckdb"), reverse=True
        )
        for stale in snapshots[2:]:
            if stale.name == keep:
                continue
            try:
                stale.unlink()
            except OSError:
                # A previous file may still be open under an in-flight request
                # (or held on Windows); the next prune retries.
                pass
