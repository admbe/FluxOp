from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

import duckdb

from api.recovery import inspect_database, recover_database_from_latest_backup


class _Blob:
    def __init__(self, name: str, content: bytes, age_minutes: int):
        self.name = name
        self.content = content
        self.size = len(content)
        self.last_modified = datetime.now(timezone.utc) - timedelta(
            minutes=age_minutes
        )


class _Download:
    def __init__(self, content: bytes):
        self.content = content

    def readinto(self, stream):
        stream.write(self.content)
        return len(self.content)


class _BlobClient:
    def __init__(self, blob: _Blob):
        self.blob = blob

    def download_blob(self, **_):
        return _Download(self.blob.content)


class _Container:
    def __init__(self, blobs: list[_Blob]):
        self.blobs = blobs

    def list_blobs(self, **_):
        return list(self.blobs)

    def get_blob_client(self, name: str):
        return _BlobClient(next(blob for blob in self.blobs if blob.name == name))


def _valid_database(path: Path, resource_count: int = 3) -> None:
    connection = duckdb.connect(str(path))
    try:
        connection.execute(
            """
            CREATE TABLE azure_integration (id VARCHAR);
            CREATE TABLE resource_snapshots (
                snapshot_id VARCHAR,
                resource_id VARCHAR
            );
            CREATE VIEW resources_current AS
                SELECT resource_id
                FROM resource_snapshots;
            """
        )
        connection.executemany(
            "INSERT INTO resource_snapshots VALUES (?, ?)",
            [(f"snapshot-{index}", f"resource-{index}") for index in range(resource_count)],
        )
        connection.execute("CHECKPOINT")
    finally:
        connection.close()


class DatabaseRecoveryTests(unittest.TestCase):
    def test_healthy_database_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "flux.duckdb"
            _valid_database(database, 4)

            result = recover_database_from_latest_backup(
                database,
                account_url="unused",
                container_name="unused",
                container_client=_Container([]),
            )

            self.assertFalse(result.restored)
            self.assertEqual(result.resource_count, 4)

    def test_latest_valid_backup_replaces_corrupt_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "flux.duckdb"
            database.write_bytes(b"not a duckdb database")
            valid = root / "valid.duckdb"
            _valid_database(valid, 5)
            blobs = [
                _Blob("duckdb/flux-newest.duckdb", b"invalid", 1),
                _Blob("duckdb/flux-valid.duckdb", valid.read_bytes(), 2),
            ]

            result = recover_database_from_latest_backup(
                database,
                account_url="unused",
                container_name="unused",
                container_client=_Container(blobs),
            )

            self.assertTrue(result.restored)
            self.assertEqual(result.backup_name, "duckdb/flux-valid.duckdb")
            self.assertEqual(inspect_database(database).resource_count, 5)
            self.assertEqual(Path(result.preserved_path).read_bytes(), b"not a duckdb database")


if __name__ == "__main__":
    unittest.main()
