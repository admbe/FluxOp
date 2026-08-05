from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import tempfile
from typing import Any

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient


def backup_database(database: Any, settings: Any) -> str:
    now = datetime.now(timezone.utc)
    blob_name = f"duckdb/flux-{now.strftime('%Y%m%dT%H%M%SZ')}.duckdb"
    credential = DefaultAzureCredential(
        managed_identity_client_id=settings.managed_identity_client_id or None
    )
    service = BlobServiceClient(
        account_url=settings.backup_storage_account_url,
        credential=credential,
    )
    container = service.get_container_client(settings.backup_container)
    try:
        container.create_container()
    except Exception as error:
        if getattr(error, "status_code", None) != 409:
            raise
    with tempfile.TemporaryDirectory(prefix="flux-backup-") as directory:
        target = Path(directory) / "flux.duckdb"
        with database.connect() as connection:
            connection.execute("CHECKPOINT")
            shutil.copy2(database.path, target)
        with target.open("rb") as stream:
            container.upload_blob(blob_name, stream, overwrite=False)

    cutoff = now - timedelta(days=max(settings.backup_retention_days, 1))
    for blob in container.list_blobs(name_starts_with="duckdb/flux-"):
        if blob.last_modified and blob.last_modified < cutoff:
            container.delete_blob(blob.name)
    return blob_name
