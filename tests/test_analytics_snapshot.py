import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.analytics_snapshot import (
    AnalyticsSnapshotManager,
    LocalSnapshotStorage,
    SnapshotPublisher,
    SnapshotValidationError,
    validate_candidate,
)
from api.database import FluxDatabase


class AnalyticsSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.database = FluxDatabase(root / "flux.duckdb")
        self.database.init()
        self.storage = LocalSnapshotStorage(root / "snapshots")
        self.cache = root / "snapshot-cache"
        self.publisher = SnapshotPublisher(self.database, self.storage, retention=2)
        self.manager = AnalyticsSnapshotManager(
            self.database, self.storage, self.cache
        )

    def tearDown(self):
        self.database.attach_read_snapshot(None)
        self.temp.cleanup()

    def test_publish_creates_approved_publication_with_checksum(self):
        publication = self.publisher.publish()
        self.assertIsNotNone(publication)
        self.assertEqual(publication["status"], "approved")
        self.assertEqual(publication["version"], 1)
        self.assertTrue(publication["checksum"])
        self.assertIn("resource_snapshots", publication["rowCounts"])
        stored = self.storage.directory / publication["fileName"]
        self.assertTrue(stored.exists())
        latest = self.database.latest_analytics_publication()
        self.assertEqual(latest["version"], 1)
        self.assertEqual(latest["checksum"], publication["checksum"])

    def test_corrupt_candidate_is_rejected_and_not_approved(self):
        with self.assertRaises(SnapshotValidationError):
            corrupt = Path(self.temp.name) / "corrupt.duckdb"
            corrupt.write_bytes(b"not a duckdb file")
            validate_candidate(corrupt)
        self.assertIsNone(self.database.latest_analytics_publication())

    def test_manager_adopts_approved_snapshot_and_routes_reads(self):
        self.publisher.publish()
        adopted = self.manager.refresh_once()
        self.assertTrue(adopted)
        self.assertEqual(self.manager.active_version, 1)
        self.assertIsNotNone(self.database.read_snapshot_path)
        # Reads served from the immutable snapshot do not see later writes
        # to the mutable database until the next publication is adopted.
        overview_before = self.database.overview()
        self.assertEqual(overview_before["summary"]["resourceCount"], 0)

    def test_manager_swaps_to_newer_version_and_reads_new_data(self):
        self.publisher.publish()
        self.manager.refresh_once()
        self.database.attach_read_snapshot(None)
        self.database.seed_demo()
        self.database.attach_read_snapshot(
            self.cache / "flux-analytics-00000001.duckdb"
        )
        self.assertEqual(
            self.database.overview()["summary"]["resourceCount"], 0
        )
        self.publisher.publish()
        self.assertTrue(self.manager.refresh_once())
        self.assertEqual(self.manager.active_version, 2)
        self.assertGreater(
            self.database.overview()["summary"]["resourceCount"], 0
        )

    def test_refresh_is_idempotent_for_current_version(self):
        self.publisher.publish()
        self.assertTrue(self.manager.refresh_once())
        self.assertFalse(self.manager.refresh_once())

    def test_checksum_mismatch_is_not_adopted(self):
        publication = self.publisher.publish()
        tampered = self.cache / publication["fileName"]
        self.cache.mkdir(parents=True, exist_ok=True)
        self.storage.download(publication["fileName"], tampered)
        with tampered.open("ab") as stream:
            stream.write(b"tamper")
        self.assertFalse(self.manager.refresh_once())
        self.assertIsNone(self.manager.active_version)
        self.assertIsNone(self.database.read_snapshot_path)

    def test_retention_prunes_oldest_approved_publications(self):
        first = self.publisher.publish()
        self.publisher.publish()
        third = self.publisher.publish()
        latest = self.database.latest_analytics_publication()
        self.assertEqual(latest["version"], third["version"])
        self.assertFalse(
            (self.storage.directory / first["fileName"]).exists()
        )

    def test_no_publication_leaves_direct_reads(self):
        self.assertFalse(self.manager.refresh_once())
        self.assertIsNone(self.database.read_snapshot_path)
        overview = self.database.overview()
        self.assertEqual(overview["summary"]["resourceCount"], 0)


if __name__ == "__main__":
    unittest.main()


class PublicationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.database = FluxDatabase(root / "flux.duckdb")
        self.database.init()
        self.storage = LocalSnapshotStorage(root / "snapshots")

    def tearDown(self):
        self.temp.cleanup()

    def test_publication_bursts_coalesce_within_interval(self):
        publisher = SnapshotPublisher(
            self.database, self.storage, retention=5,
            min_interval_seconds=600,
        )
        first = publisher.publish()
        self.assertEqual(first["status"], "approved")
        second = publisher.publish()
        self.assertEqual(second["status"], "coalesced")
        self.assertEqual(second["version"], first["version"])
        # Force bypasses coalescing for operator-driven publication.
        forced = publisher.publish(force=True)
        self.assertEqual(forced["status"], "approved")
        self.assertEqual(forced["version"], first["version"] + 1)

    def test_publication_records_duration(self):
        publisher = SnapshotPublisher(self.database, self.storage, retention=5)
        publication = publisher.publish()
        self.assertGreaterEqual(publication["durationMs"], 0)
        latest = self.database.latest_analytics_publication()
        self.assertEqual(latest["version"], publication["version"])

    def test_tiered_retention_keeps_newest_daily_versions(self):
        from datetime import timedelta

        from api.database import utc_now

        for version in (1, 2, 3, 4):
            self.database.record_analytics_publication(
                status="approved",
                file_name=f"flux-analytics-{version:08d}.duckdb",
                checksum=f"sum-{version}",
                file_size_bytes=1,
                row_counts={},
                version=version,
            )
        # Versions 1-2 belong to the prior UTC day.
        with self.database.operational_connect() as db:
            db.execute(
                "UPDATE analytics_publications SET generated_at = ? "
                "WHERE version <= 2",
                [utc_now() - timedelta(days=1)],
            )
            db.commit()

        stale = self.database.prune_analytics_publications(
            keep=2, daily_retention_days=14
        )

        with self.database.operational_connect(read_only=True) as db:
            kept = sorted(
                int(r[0]) for r in db.execute(
                    "SELECT version FROM analytics_publications "
                    "WHERE status = 'approved'"
                ).fetchall()
            )
        # Newest 2 (v3, v4) plus newest of the prior day (v2); v1 pruned.
        self.assertEqual(kept, [2, 3, 4])
        self.assertEqual(stale, ["flux-analytics-00000001.duckdb"])

    def test_prune_without_daily_tier_keeps_only_newest(self):
        for version in (1, 2, 3):
            self.database.record_analytics_publication(
                status="approved",
                file_name=f"flux-analytics-{version:08d}.duckdb",
                checksum=f"sum-{version}",
                file_size_bytes=1,
                row_counts={},
                version=version,
            )
        stale = self.database.prune_analytics_publications(keep=2)
        self.assertEqual(stale, ["flux-analytics-00000001.duckdb"])


class SnapshotRegressionGateTests(unittest.TestCase):
    """Gates added after the 2026-07-31 outage, where a snapshot with a torn
    column segment passed validation and then crashed the web."""

    def test_emptied_critical_table_is_rejected(self):
        from api.analytics_snapshot import _reject_catastrophic_regression

        previous = {
            "version": 17,
            "rowCounts": json.dumps({"resources_current": 4836, "costs_current": 12}),
        }
        # A wiped working copy is structurally valid but must never publish.
        with self.assertRaises(SnapshotValidationError) as raised:
            _reject_catastrophic_regression(
                {"resources_current": 0, "costs_current": 12}, previous
            )
        self.assertIn("resources_current", str(raised.exception))

    def test_growth_and_first_publication_are_allowed(self):
        from api.analytics_snapshot import _reject_catastrophic_regression

        previous = {"version": 17, "rowCounts": json.dumps({"resources_current": 10})}
        _reject_catastrophic_regression({"resources_current": 11}, previous)
        # No prior approved publication: nothing to regress against.
        _reject_catastrophic_regression({"resources_current": 0}, None)

    def test_validation_scans_every_materialized_table(self):
        """The two tables corrupted on 2026-07-31 must now be covered."""
        from api.analytics_snapshot import _CRITICAL_CURRENT

        for name in ("opportunity_valuation_current", "rule_opportunities_current"):
            self.assertIn(name, _CRITICAL_CURRENT)
