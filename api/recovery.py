from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient
import duckdb
from filelock import FileLock


@dataclass(frozen=True)
class DatabaseInspection:
    resource_count: int
    table_count: int


@dataclass(frozen=True)
class DatabaseRecovery:
    restored: bool
    resource_count: int
    backup_name: str = ""
    preserved_path: str = ""


def inspect_database(path: Path) -> DatabaseInspection:
    """Open a database read-only and verify the minimum usable Flux dataset."""
    connection = duckdb.connect(str(path), read_only=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'main'
                """
            ).fetchall()
        }
        required = {"azure_integration", "resource_snapshots"}
        missing = sorted(required - tables)
        if missing:
            raise RuntimeError(
                "Backup is missing required Flux tables: " + ", ".join(missing)
            )
        resource_count = int(
            connection.execute("SELECT count(*) FROM resources_current").fetchone()[0]
        )
        if resource_count <= 0:
            raise RuntimeError("Backup contains no current Azure resources.")
        return DatabaseInspection(
            resource_count=resource_count,
            table_count=len(tables),
        )
    finally:
        connection.close()


def _backup_client(
    account_url: str,
    container_name: str,
    managed_identity_client_id: str,
):
    credential = ManagedIdentityCredential(
        client_id=managed_identity_client_id or None
    )
    service = BlobServiceClient(
        account_url=account_url,
        credential=credential,
    )
    return service.get_container_client(container_name), credential


def recover_database_from_latest_backup(
    database_path: Path,
    *,
    account_url: str,
    container_name: str,
    managed_identity_client_id: str = "",
    container_client: Any | None = None,
    maximum_candidates: int = 8,
) -> DatabaseRecovery:
    """Restore only when the current database is unreadable.

    Candidates are downloaded and validated separately. The damaged database
    is retained with a timestamped suffix, and promotion uses an atomic rename
    on the same persistent volume.
    """
    database_path = Path(database_path)
    if database_path.exists():
        try:
            inspection = inspect_database(database_path)
            return DatabaseRecovery(
                restored=False,
                resource_count=inspection.resource_count,
            )
        except Exception as error:
            print(
                "Flux database recovery requested because the current database "
                f"failed validation: {type(error).__name__}: {error}"
            )
    else:
        raise RuntimeError(
            f"Flux database recovery refused because {database_path} does not exist."
        )

    if not account_url:
        raise RuntimeError("Flux backup storage account URL is not configured.")

    database_path.parent.mkdir(parents=True, exist_ok=True)
    credential = None
    if container_client is None:
        container_client, credential = _backup_client(
            account_url,
            container_name,
            managed_identity_client_id,
        )

    try:
        candidates = sorted(
            (
                blob
                for blob in container_client.list_blobs(
                    name_starts_with="duckdb/flux-"
                )
                if int(getattr(blob, "size", 0) or 0) > 0
            ),
            key=lambda blob: blob.last_modified,
            reverse=True,
        )[: max(maximum_candidates, 1)]
        if not candidates:
            raise RuntimeError("No Flux DuckDB backups are available.")

        failures: list[str] = []
        with FileLock(str(database_path) + ".writer.lock").acquire(timeout=180):
            # Another process may have completed recovery while this process
            # waited for the cross-process lease.
            try:
                inspection = inspect_database(database_path)
                return DatabaseRecovery(
                    restored=False,
                    resource_count=inspection.resource_count,
                )
            except Exception:
                pass

            for blob in candidates:
                temporary = database_path.with_name(
                    f".{database_path.name}.restore-{uuid4().hex}.tmp"
                )
                try:
                    with temporary.open("wb") as stream:
                        container_client.get_blob_client(blob.name).download_blob(
                            max_concurrency=1
                        ).readinto(stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    inspection = inspect_database(temporary)
                except Exception as error:
                    failures.append(
                        f"{blob.name}: {type(error).__name__}: {error}"
                    )
                    temporary.unlink(missing_ok=True)
                    continue

                timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                preserved = database_path.with_name(
                    f"{database_path.name}.corrupt-{timestamp}"
                )
                preserved_wal = Path(str(preserved) + ".wal")
                current_wal = Path(str(database_path) + ".wal")
                os.replace(database_path, preserved)
                if current_wal.exists():
                    os.replace(current_wal, preserved_wal)
                try:
                    os.replace(temporary, database_path)
                except Exception:
                    os.replace(preserved, database_path)
                    if preserved_wal.exists():
                        os.replace(preserved_wal, current_wal)
                    raise

                return DatabaseRecovery(
                    restored=True,
                    resource_count=inspection.resource_count,
                    backup_name=blob.name,
                    preserved_path=str(preserved),
                )

        first_failures = "; ".join(failures[:3])
        raise RuntimeError(
            "No valid Flux DuckDB backup was found."
            + (f" First failures: {first_failures}" if first_failures else "")
        )
    finally:
        close = getattr(credential, "close", None)
        if close:
            close()
