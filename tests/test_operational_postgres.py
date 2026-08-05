from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import tempfile
import unittest

from api.database import FluxDatabase
from api.operational_migration import migrate_operational_state
from api.operational_store import _translate_sql


POSTGRES_URL = os.getenv("FLUX_TEST_POSTGRES_URL", "").strip()
OPERATIONAL_TABLES = (
    "cost_management_quota_state",
    "cost_details_backfill_scopes",
    "cost_history_request_attempts",
    "cost_history_scope_runs",
    "cost_history_runs",
    "source_sync_state",
    "cost_anomaly_reviews",
    "intelligence_transcript_events",
    "intelligence_usage_events",
    "focus_import_runs",
    "sync_source_runs",
    "sync_runs",
    "azure_integration",
    "schema_migrations",
)


class PostgreSQLTranslationTests(unittest.TestCase):
    def test_duckdb_double_cast_is_translated_for_postgres(self) -> None:
        translated = _translate_sql(
            "SELECT CAST(NULL AS DOUBLE), value FROM sample WHERE id = ?",
            "postgres",
        )
        self.assertEqual(
            translated,
            "SELECT CAST(NULL AS DOUBLE PRECISION), value "
            "FROM sample WHERE id = %s",
        )


@unittest.skipUnless(
    POSTGRES_URL,
    "Set FLUX_TEST_POSTGRES_URL to run PostgreSQL integration tests.",
)
class PostgreSQLOperationalStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database = FluxDatabase(
            root / "analytics.duckdb",
            operational_database_url=POSTGRES_URL,
            operational_duckdb_path=root / "unused-operational.duckdb",
        )
        self.database.init()
        with self.database.operational_connect() as connection:
            connection.execute(
                f"TRUNCATE TABLE {', '.join(OPERATIONAL_TABLES)}"
            )
        self.database._operational.init()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_sync_lifecycle_uses_postgres(self) -> None:
        self.database.save_integration(
            {
                "name": "Azure",
                "tenantId": "tenant-1",
                "enabled": True,
                "authMode": "managed_identity",
                "subscriptions": [
                    {"subscriptionId": "sub-1", "label": "Production"}
                ],
            }
        )
        sync_id = self.database.start_sync("managed_identity")
        claimed = self.database.claim_next_sync()
        self.assertEqual(claimed["id"], sync_id)

        self.database.begin_sync_source(sync_id, "inventory", "sub-1")
        self.database.record_sync_source_attempt(
            sync_id,
            "inventory",
            "sub-1",
            {
                "attemptNumber": 1,
                "statusCode": 429,
                "retryAfterSeconds": 30,
            },
        )
        self.database.finish_sync_source(
            sync_id,
            "inventory",
            "sub-1",
            "succeeded",
            12,
            "Collected inventory.",
        )
        self.database.finish_sync(
            sync_id,
            "succeeded",
            "Synchronization complete.",
            12,
        )

        latest = self.database.latest_sync()
        self.assertEqual(latest["id"], sync_id)
        self.assertEqual(latest["status"], "succeeded")
        self.assertEqual(self.database.operational_backend, "postgres")

    def test_cost_history_retry_state_uses_postgres(self) -> None:
        self.database.save_integration(
            {
                "name": "Azure",
                "tenantId": "tenant-1",
                "enabled": True,
                "authMode": "managed_identity",
                "subscriptions": [
                    {"subscriptionId": "sub-1", "label": "Production"}
                ],
            }
        )
        self.database.start_cost_history_run("cost-run-1", 1)
        self.database.begin_cost_history_scope(
            "cost-run-1",
            "sub-1",
            "ActualCost",
            date(2026, 7, 1),
            date(2026, 7, 27),
        )
        self.database.record_cost_history_request_attempt(
            "cost-run-1",
            "sub-1",
            "ActualCost",
            {
                "attemptNumber": 1,
                "status": "retrying",
                "statusCode": 429,
                "retryAfterSeconds": 20,
                "message": "Throttled",
            },
        )
        self.database.finish_cost_history_scope(
            "cost-run-1",
            "sub-1",
            "ActualCost",
            status="failed",
            status_code=429,
            message="Throttled",
        )
        self.database.finish_cost_history_run(
            "cost-run-1",
            status="failed",
            completed_scopes=0,
            failed_scopes=1,
            row_count=0,
            message="Retry pending.",
        )

        status = self.database.cost_history_status()
        self.assertEqual(status["latestRun"]["runId"], "cost-run-1")
        self.assertEqual(status["scopes"][0]["retryCount"], 1)
        self.assertEqual(status["scopes"][0]["retryAfterSeconds"], 20)

    def test_intelligence_usage_upserts_and_reports_from_postgres(self) -> None:
        payload = {
            "request_id": "request-1",
            "user_hash": "user-hash",
            "provider": "provider",
            "model": "model",
            "status": "succeeded",
            "latency_ms": 120,
            "prompt_tokens": 10,
            "cached_prompt_tokens": 0,
            "completion_tokens": 20,
            "estimated_cost_usd": 0.001,
            "tool_names": ["cost_summary"],
            "transcript_retention_days": 30,
            "messages": [{"role": "user", "content": "Show cost."}],
            "context": {"page": "reports"},
            "response": {"answer": "Cost summary."},
        }
        self.database.record_intelligence_usage(**payload)
        payload["latency_ms"] = 100
        self.database.record_intelligence_usage(**payload)

        status = self.database.intelligence_usage_status()
        self.assertEqual(status["requestCount"], 1)
        self.assertEqual(status["successfulRequestCount"], 1)
        self.assertTrue(
            self.database.record_intelligence_client_performance(
                request_id="request-1",
                user_hash="user-hash",
                client_round_trip_ms=180,
                client_render_ms=20,
                client_end_to_end_ms=200,
            )
        )
        review = self.database.intelligence_transcript_review()
        self.assertEqual(review["items"][0]["requestId"], "request-1")
        self.assertEqual(
            review["items"][0]["performance"]["clientEndToEndMs"],
            200,
        )
        self.assertTrue(
            self.database.record_intelligence_feedback(
                "request-1",
                "helpful",
                "Accurate.",
            )
        )

    def test_focus_import_lifecycle_uses_postgres(self) -> None:
        self.database.start_focus_import("focus-run-1")
        self.database.start_focus_import("focus-run-1")
        self.database.finish_focus_import(
            "focus-run-1",
            status="succeeded",
            manifest_count=2,
            charge_count=50,
        )
        with self.database.operational_connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT status, manifest_count, charge_count
                FROM focus_import_runs
                WHERE run_id = ?
                """,
                ["focus-run-1"],
            ).fetchone()
        self.assertEqual(tuple(row), ("succeeded", 2, 50))

    def test_legacy_operational_state_migrates_with_row_validation(self) -> None:
        source_path = Path(self.temp.name) / "legacy.duckdb"
        source = FluxDatabase(source_path)
        source.init()
        with source.connect() as connection:
            connection.execute(
                """
                UPDATE azure_integration
                SET tenant_id = 'tenant-migrated',
                    auth_mode = 'managed_identity'
                WHERE id = 'azure'
                """
            )
            connection.execute(
                """
                INSERT INTO sync_runs (
                    id, provider, started_at, completed_at, status,
                    resource_count, message, trigger, stage, stage_message,
                    claimed_at, requested_sources_json
                ) VALUES (
                    'sync-migrated', 'managed_identity', current_timestamp,
                    current_timestamp, 'succeeded', 42, 'Complete.', 'manual',
                    'complete', 'Complete.', current_timestamp, '["inventory"]'
                )
                """
            )

        results = migrate_operational_state(
            source_path,
            POSTGRES_URL,
            replace=True,
        )
        counts = {result.table: result.row_count for result in results}
        self.assertEqual(counts["azure_integration"], 1)
        self.assertEqual(counts["sync_runs"], 1)
        with self.database.operational_connect(read_only=True) as connection:
            integration = connection.execute(
                """
                SELECT tenant_id, auth_mode
                FROM azure_integration
                WHERE id = 'azure'
                """
            ).fetchone()
            sync = connection.execute(
                """
                SELECT status, resource_count
                FROM sync_runs
                WHERE id = 'sync-migrated'
                """
            ).fetchone()
        self.assertEqual(tuple(integration), ("tenant-migrated", "managed_identity"))
        self.assertEqual(tuple(sync), ("succeeded", 42))


class ConnectionPoolTests(unittest.TestCase):
    """The capacity semaphore must never leak permits across checkouts."""

    class _FakeConnection:
        def cursor(self):
            return self

        def execute(self, *args):
            return self

        def close(self):
            pass

        def commit(self):
            pass

        def rollback(self):
            pass

    def _pooled_store(self, size: int):
        import threading
        from unittest.mock import patch

        from api.operational_store import OperationalStore

        store = OperationalStore(
            database_url="postgresql://user:pw@localhost/flux",
            duckdb_path=Path(tempfile.gettempdir()) / "pool-test.duckdb",
        )
        store._pool_size = size
        store._capacity = threading.BoundedSemaphore(size)
        store._acquire_timeout = 0.5
        patches = [
            patch.object(
                OperationalStore,
                "_new_connection",
                lambda self: ConnectionPoolTests._FakeConnection(),
            ),
            patch.object(OperationalStore, "_ping", lambda self, c: True),
        ]
        return store, patches

    def test_sequential_reuse_never_exhausts_capacity(self) -> None:
        store, patches = self._pooled_store(size=2)
        for item in patches:
            item.start()
        try:
            for _ in range(10):
                with store.connect(read_only=True):
                    pass
        finally:
            for item in patches:
                item.stop()

    def test_exhausted_pool_raises_instead_of_hanging(self) -> None:
        from api.operational_store import OperationalPoolTimeout

        store, patches = self._pooled_store(size=1)
        for item in patches:
            item.start()
        try:
            with store.connect(read_only=True):
                with self.assertRaises(OperationalPoolTimeout):
                    with store.connect(read_only=True):
                        self.fail("A second connection exceeded the pool bound.")
            # The permit returns after checkin; the pool must work again.
            with store.connect(read_only=True):
                pass
        finally:
            for item in patches:
                item.stop()

    def test_failed_connection_creation_releases_permit(self) -> None:
        import threading
        from unittest.mock import patch

        from api.operational_store import OperationalStore

        store = OperationalStore(
            database_url="postgresql://user:pw@localhost/flux",
            duckdb_path=Path(tempfile.gettempdir()) / "pool-test.duckdb",
        )
        store._pool_size = 1
        store._capacity = threading.BoundedSemaphore(1)
        store._acquire_timeout = 0.5

        def broken(self):
            raise ConnectionError("server unavailable")

        with patch.object(OperationalStore, "_new_connection", broken):
            for _ in range(3):
                with self.assertRaises(ConnectionError):
                    with store.connect(read_only=True):
                        pass


class CoordinationPrimitiveTests(unittest.TestCase):
    """Singleton leases and shared throttle state on the DuckDB fallback."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        from api.operational_store import OperationalStore

        self.store = OperationalStore(
            duckdb_path=Path(self._temp.name) / "op.duckdb",
        )
        self.store.init()

    def tearDown(self):
        self.store.close()
        self._temp.cleanup()

    def test_singleton_lease_excludes_second_holder(self):
        from api.operational_store import SingletonLeaseUnavailable

        with self.store.singleton_lease("job-a"):
            with self.assertRaises(SingletonLeaseUnavailable):
                with self.store.singleton_lease("job-a"):
                    self.fail("Second holder acquired the lease.")
        # Released leases are reacquirable.
        with self.store.singleton_lease("job-a"):
            pass

    def test_distinct_lease_names_are_independent(self):
        with self.store.singleton_lease("job-a"):
            with self.store.singleton_lease("job-b"):
                pass

    def test_throttle_slot_spacing_is_shared(self):
        first = self.store.claim_throttle_slot("gate", 30)
        self.assertEqual(first, 0.0)
        wait = self.store.claim_throttle_slot("gate", 30)
        self.assertGreater(wait, 25)
        self.assertLessEqual(wait, 30)

    def test_throttle_cooldown_pushes_next_slot_out(self):
        self.store.claim_throttle_slot("gate", 1)
        self.store.register_throttle_cooldown("gate", 120)
        wait = self.store.claim_throttle_slot("gate", 1)
        self.assertGreater(wait, 100)

    def test_cooldown_never_shortens_existing_block(self):
        self.store.claim_throttle_slot("gate", 300)
        self.store.register_throttle_cooldown("gate", 1)
        wait = self.store.claim_throttle_slot("gate", 300)
        self.assertGreater(wait, 200)

    def test_rolling_qpu_budget_blocks_until_window_expires(self):
        windows = [(10, 1), (60, 10), (3600, 100)]
        wait, reservation = self.store.claim_cost_management_quota(
            "tenant-a", 1, windows
        )
        self.assertEqual(wait, 0.0)
        self.assertTrue(reservation)
        wait, second = self.store.claim_cost_management_quota(
            "tenant-a", 1, windows
        )
        self.assertGreater(wait, 8)
        self.assertIsNone(second)

    def test_qpu_reconciliation_updates_active_reservation(self):
        windows = [(10, 4), (60, 10), (3600, 100)]
        _, reservation = self.store.claim_cost_management_quota(
            "tenant-b", 1, windows
        )
        self.store.reconcile_cost_management_quota(
            "tenant-b", reservation, consumed_qpu=4
        )
        wait, second = self.store.claim_cost_management_quota(
            "tenant-b", 1, windows
        )
        self.assertGreater(wait, 8)
        self.assertIsNone(second)

    def test_server_cooldown_is_part_of_quota_state(self):
        self.store.register_cost_management_quota_cooldown("tenant-c", 120)
        wait, reservation = self.store.claim_cost_management_quota(
            "tenant-c", 1, [(10, 6), (60, 30), (3600, 300)]
        )
        self.assertGreater(wait, 100)
        self.assertIsNone(reservation)
