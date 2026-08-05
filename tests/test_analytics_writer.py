from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.analytics_writer import (
    APPLIERS,
    StagedPayloadConflict,
    apply_pending,
    stage_payload,
)
from api.database import FluxDatabase


class AnalyticsWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.database = FluxDatabase(root / "flux.duckdb")
        self.database.init()
        self.staging = root / "staging"
        self.applied: list[dict] = []

        def test_applier(database, payload):
            if payload.get("explode"):
                raise RuntimeError("applier failure")
            self.applied.append(payload)
            return {"rows": len(payload.get("items", []))}

        APPLIERS["test-source"] = test_applier

    def tearDown(self):
        APPLIERS.pop("test-source", None)
        self.temp.cleanup()

    def _job_rows(self):
        with self.database.operational_connect(read_only=True) as db:
            return db.execute(
                """
                SELECT job_id, status, attempts, error, payload_path
                FROM analytics_apply_jobs ORDER BY created_at
                """
            ).fetchall()

    def test_stage_and_apply_roundtrip(self):
        job_id = stage_payload(
            self.database, self.staging, "test-source", "key-1",
            {"items": [1, 2, 3]},
        )
        self.assertTrue(job_id.startswith("apply-"))
        applied = apply_pending(self.database, self.staging)
        self.assertEqual(applied, 1)
        self.assertEqual(self.applied, [{"items": [1, 2, 3]}])
        rows = self._job_rows()
        self.assertEqual(rows[0][1], "applied")
        # The applied payload file is removed.
        self.assertFalse(Path(rows[0][4]).exists())

    def test_duplicate_delivery_is_a_noop(self):
        first = stage_payload(
            self.database, self.staging, "test-source", "key-1", {"items": [1]}
        )
        second = stage_payload(
            self.database, self.staging, "test-source", "key-1", {"items": [1]}
        )
        self.assertEqual(first, second)
        self.assertEqual(apply_pending(self.database, self.staging), 1)
        self.assertEqual(len(self.applied), 1)
        # Re-delivery after apply also stays a no-op.
        third = stage_payload(
            self.database, self.staging, "test-source", "key-1", {"items": [1]}
        )
        self.assertEqual(third, first)
        self.assertEqual(apply_pending(self.database, self.staging), 0)

    def test_same_key_different_content_fails_closed(self):
        stage_payload(
            self.database, self.staging, "test-source", "key-1", {"items": [1]}
        )
        with self.assertRaises(StagedPayloadConflict):
            stage_payload(
                self.database, self.staging, "test-source", "key-1",
                {"items": [999]},
            )

    def test_failed_apply_retries_and_preserves_payload(self):
        stage_payload(
            self.database, self.staging, "test-source", "key-1",
            {"explode": True},
        )
        self.assertEqual(apply_pending(self.database, self.staging), 0)
        rows = self._job_rows()
        self.assertEqual(rows[0][1], "staged")
        self.assertEqual(rows[0][2], 1)
        self.assertIn("applier failure", rows[0][3])
        self.assertTrue(Path(rows[0][4]).exists())

    def test_unregistered_source_eventually_fails_terminal(self):
        stage_payload(
            self.database, self.staging, "unknown-source", "key-1", {"x": 1}
        )
        for _ in range(5):
            apply_pending(self.database, self.staging)
        rows = self._job_rows()
        self.assertEqual(rows[0][1], "failed")
        self.assertEqual(rows[0][2], 5)

    def test_retail_prices_applier_stores_snapshot(self):
        payload = {
            "snapshotId": "retail-test-1",
            "prices": [],
            "complete": True,
        }
        stage_payload(
            self.database, self.staging, "retail-prices", "retail-key", payload
        )
        self.assertEqual(apply_pending(self.database, self.staging), 1)

    def test_identical_price_collections_stage_without_conflict(self):
        # Two runs collecting byte-identical prices must both apply: source
        # freshness only advances on apply, and the collection-scoped key
        # is what keeps an unchanged catalogue from failing closed forever.
        from datetime import datetime, timedelta, timezone

        from api.pricing import retail_price_stage_parts

        prices = [
            {
                "region": "westus3",
                "targetSku": "Standard_D4s_v5",
                "status": "matched",
                "hourlyPrice": 0.192,
            }
        ]
        first_at = datetime(2026, 8, 3, 1, 15, tzinfo=timezone.utc)
        key_one, body_one = retail_price_stage_parts(
            prices, complete=True, collected_at=first_at
        )
        key_two, body_two = retail_price_stage_parts(
            prices,
            complete=True,
            collected_at=first_at + timedelta(hours=6),
        )
        self.assertNotEqual(key_one, key_two)
        self.assertEqual(body_one["prices"], body_two["prices"])
        stage_payload(
            self.database, self.staging, "retail-prices", key_one, body_one
        )
        self.assertEqual(apply_pending(self.database, self.staging), 1)
        stage_payload(
            self.database, self.staging, "retail-prices", key_two, body_two
        )
        self.assertEqual(apply_pending(self.database, self.staging), 1)
        # Redelivering the same collection stays a no-op instead of a
        # conflict: the payload is a pure function of the key.
        stage_payload(
            self.database, self.staging, "retail-prices", key_two, body_two
        )
        self.assertEqual(apply_pending(self.database, self.staging), 0)


if __name__ == "__main__":
    unittest.main()
