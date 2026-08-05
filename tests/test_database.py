from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from filelock import Timeout

from api.database import DatabaseBusyError, FluxDatabase, utc_now


class FluxDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "test.duckdb")
        self.database.init()

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_overview(self):
        overview = self.database.overview()
        self.assertEqual(overview["summary"]["resourceCount"], 0)
        self.assertEqual(overview["resourcesByType"], [])

    def test_writer_lease_is_shared_across_database_instances(self):
        competing = FluxDatabase(self.database.path)
        with self.database.writer_lease():
            with self.assertRaises(Timeout):
                with competing.writer_lease(timeout=0):
                    self.fail("A competing writer acquired the DuckDB lease.")
            run_id = self.database.start_telemetry_run("test", "test")
            self.database.finish_telemetry_run(run_id, "succeeded", 0, "ok")

    def test_read_connection_blocks_competing_process_connection(self):
        competing = FluxDatabase(self.database.path)
        entered = threading.Event()
        finished = threading.Event()

        def open_competing_connection():
            entered.set()
            with competing.connect():
                finished.set()

        with self.database.connect(read_only=True):
            thread = threading.Thread(target=open_competing_connection)
            thread.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(finished.wait(0.1))
        self.assertTrue(finished.wait(2))
        thread.join(timeout=2)

    def test_bounded_connect_fails_fast_when_lease_is_held(self):
        bounded = FluxDatabase(self.database.path, connect_timeout_seconds=0.2)
        with self.database.writer_lease():
            with self.assertRaises(DatabaseBusyError):
                with bounded.connect(read_only=True):
                    self.fail(
                        "A bounded connection acquired the lease held by "
                        "another database instance."
                    )

    def test_unbounded_connect_waits_for_lease_release(self):
        competing = FluxDatabase(self.database.path)
        acquired = threading.Event()
        release = threading.Event()

        def hold_until_released():
            with self.database.writer_lease(timeout=5):
                acquired.set()
                release.wait(5)

        thread = threading.Thread(target=hold_until_released)
        thread.start()
        try:
            self.assertTrue(acquired.wait(2))
            timer = threading.Timer(0.3, release.set)
            timer.start()
            with competing.connect(read_only=True):
                pass
        finally:
            release.set()
            thread.join(timeout=5)

    def test_duckdb_connections_use_memory_friendly_settings(self):
        with TemporaryDirectory() as temp:
            with patch.dict(
                "os.environ",
                {
                    "FLUX_DUCKDB_MEMORY_LIMIT": "1GB",
                    "FLUX_DUCKDB_MAX_TEMP_DIRECTORY_SIZE": "8GB",
                },
            ):
                database = FluxDatabase(Path(temp) / "limited.duckdb")
            database.init()
            with database.connect(read_only=True) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT current_setting('threads')"
                    ).fetchone()[0],
                    1,
                )
                self.assertFalse(
                    db.execute(
                        "SELECT current_setting('preserve_insertion_order')"
                    ).fetchone()[0]
                )
                self.assertEqual(
                    db.execute(
                        "SELECT current_setting('memory_limit')"
                    ).fetchone()[0],
                    "953.6 MiB",
                )
                self.assertEqual(
                    Path(
                        db.execute(
                            "SELECT current_setting('temp_directory')"
                        ).fetchone()[0]
                    ),
                    database.path.parent / ".duckdb-tmp",
                )
                self.assertEqual(
                    db.execute(
                        "SELECT current_setting('max_temp_directory_size')"
                    ).fetchone()[0],
                    "7.4 GiB",
                )

    def test_disabled_focus_cost_is_not_reported_as_incomplete_or_stale(self):
        with TemporaryDirectory() as temp:
            database = FluxDatabase(
                Path(temp) / "focus-disabled.duckdb",
                focus_cost_enabled=False,
            )
            database.init()

            self.assertNotIn(
                "FocusCost",
                {
                    dataset["source"]
                    for dataset in database.cost_reconciliation()["datasets"]
                },
            )
            self.assertNotIn(
                "FocusCost",
                {
                    source["source"]
                    for source in database.source_freshness()
                },
            )

    def test_optional_focus_cost_is_hidden_until_data_exists(self):
        with TemporaryDirectory() as temp:
            database = FluxDatabase(
                Path(temp) / "focus-optional.duckdb",
                focus_cost_enabled=True,
                focus_cost_required=False,
            )
            database.init()

            self.assertNotIn(
                "FocusCost",
                {
                    dataset["source"]
                    for dataset in database.cost_reconciliation()["datasets"]
                },
            )
            self.assertNotIn(
                "FocusCost",
                {
                    source["source"]
                    for source in database.source_freshness()
                },
            )

    def test_operational_state_uses_legacy_duckdb_by_default(self):
        self.assertEqual(self.database.operational_backend, "duckdb")
        self.assertEqual(self.database._operational.duckdb_path, self.database.path)

    def test_cost_history_status_includes_persisted_request_retries(self):
        run_id = "history-observability"
        self.database.start_cost_history_run(run_id, 1)
        self.database.begin_cost_history_scope(
            run_id,
            "sub-1",
            "AmortizedCost",
            date(2026, 7, 1),
            date(2026, 7, 25),
        )
        self.database.record_cost_history_request_attempt(
            run_id,
            "sub-1",
            "AmortizedCost",
            {
                "attemptNumber": 1,
                "status": "retrying",
                "statusCode": 429,
                "retryAfterSeconds": 5,
                "message": "throttled",
            },
        )
        self.database.record_cost_history_request_attempt(
            run_id,
            "sub-1",
            "AmortizedCost",
            {
                "attemptNumber": 2,
                "status": "succeeded",
                "statusCode": 200,
                "message": "ok",
            },
        )
        self.database.finish_cost_history_scope(
            run_id,
            "sub-1",
            "AmortizedCost",
            status="succeeded",
        )
        self.database.finish_cost_history_run(
            run_id,
            status="succeeded",
            completed_scopes=1,
            failed_scopes=0,
            row_count=0,
            message="complete",
        )

        status = self.database.cost_history_status()

        self.assertEqual(status["scopes"][0]["attemptCount"], 2)
        self.assertEqual(status["scopes"][0]["retryCount"], 1)
        self.assertIsNotNone(status["scopes"][0]["lastAttemptAt"])
        self.assertIsNotNone(status["scopes"][0]["nextRetryAt"])
        self.assertEqual(status["scopes"][0]["retryAfterSeconds"], 5)

    def test_cost_details_backfill_checkpoints_calendar_months(self):
        first = self.database.next_cost_details_backfill_period(
            "SUB-1",
            "ActualCost",
            initial_days=90,
            current_refresh_days=7,
            as_of=date(2026, 7, 25),
        )

        self.assertEqual(first, (date(2026, 7, 1), date(2026, 7, 25)))
        self.database.begin_cost_details_backfill(
            "sub-1",
            "ActualCost",
            first[0],
            first[1],
        )
        self.database.finish_cost_details_backfill(
            "sub-1",
            "ActualCost",
            first[0],
            status="succeeded",
            row_count=12,
            message="complete",
        )

        second = self.database.next_cost_details_backfill_period(
            "sub-1",
            "ActualCost",
            initial_days=90,
            current_refresh_days=7,
            as_of=date(2026, 7, 25),
        )
        status = self.database.cost_history_status()

        self.assertEqual(second, (date(2026, 6, 1), date(2026, 6, 30)))
        self.assertEqual(status["backfill"]["completedPeriods"], 1)
        self.assertEqual(status["backfill"]["failedPeriods"], 0)
        self.assertEqual(
            status["backfill"]["periods"][0]["source"],
            "azure_cost_details_report",
        )

    def test_daily_cost_scope_is_batched_for_large_windows(self):
        records = [
            {
                "usageDate": date(2026, 7, 26),
                "resourceId": f"/subscriptions/sub-1/resources/{index}",
                "serviceName": f"Service-{index}",
                "amount": float(index),
                "currency": "USD",
            }
            for index in range(1205)
        ]

        inserted = self.database.store_daily_cost_scope(
            "batch-run",
            "sub-1",
            "ActualCost",
            records,
            start_date=date(2026, 7, 26),
            end_date=date(2026, 7, 26),
        )

        self.assertEqual(inserted, 1205)
        with self.database.connect(read_only=True) as db:
            self.assertEqual(
                db.execute(
                    """
                    SELECT count(*)
                    FROM daily_cost_history
                    WHERE subscription_id = 'sub-1'
                      AND cost_type = 'ActualCost'
                    """
                ).fetchone()[0],
                1205,
            )

    def test_daily_cost_refresh_requires_a_complete_initial_scope(self):
        as_of = date(2026, 7, 26)
        initial_start = date(2026, 4, 28)
        self.database.store_daily_cost_scope(
            "partial-window",
            "sub-1",
            "ActualCost",
            [
                {
                    "usageDate": as_of,
                    "resourceId": "/subscriptions/sub-1",
                    "serviceName": "Support",
                    "amount": 1,
                    "currency": "USD",
                }
            ],
            start_date=as_of,
            end_date=as_of,
        )

        self.assertEqual(
            self.database.daily_cost_query_start(
                "sub-1",
                "ActualCost",
                initial_days=90,
                refresh_days=14,
                as_of=as_of,
            ),
            initial_start,
        )

        self.database.start_cost_history_run("complete-backfill", 1)
        self.database.begin_cost_history_scope(
            "complete-backfill",
            "sub-1",
            "ActualCost",
            initial_start,
            as_of,
        )
        self.database.finish_cost_history_scope(
            "complete-backfill",
            "sub-1",
            "ActualCost",
            status="succeeded",
            row_count=1,
        )

        self.assertEqual(
            self.database.daily_cost_query_start(
                "sub-1",
                "ActualCost",
                initial_days=90,
                refresh_days=14,
                as_of=as_of,
            ),
            date(2026, 7, 13),
        )

    def test_recommendation_quality_detects_semantic_repetition(self):
        self.database.seed_demo()
        resource_id = (
            "/subscriptions/00000000-0000-0000-0000-000000000001/"
            "resourceGroups/flux-demo/providers/"
            "microsoft.compute/virtualmachines/checkout-api"
        )
        recommendations = [
            {
                "recommendationId": recommendation_id,
                "resourceId": resource_id,
                "subscriptionId": "00000000-0000-0000-0000-000000000001",
                "subscriptionName": "Platform Production",
                "resourceType": "microsoft.compute/virtualmachines",
                "category": "Cost",
                "impact": "High",
                "problem": "The VM is underutilized.",
                "solution": "Resize the VM.",
                "recommendationTypeId": "resize",
                "recommendedSku": "Standard_D2s_v5",
                "raw": {},
            }
            for recommendation_id in ("advisor-a", "advisor-b")
        ]
        self.database.store_snapshot(
            "quality",
            [],
            inventory_collected=False,
            advisor=recommendations,
            advisor_collected=True,
        )

        quality = self.database.recommendation_quality()
        health = self.database.operational_health()

        self.assertEqual(quality["storedActive"], 2)
        self.assertEqual(quality["semanticActions"], 1)
        self.assertEqual(quality["semanticDuplicates"], 1)
        self.assertEqual(quality["status"], "failed")
        self.assertEqual(health["recommendations"]["status"], "failed")
        self.assertEqual(health["worker"]["status"], "ready")

    def test_operational_health_marks_a_stalled_worker_critical(self):
        sync_id = self.database.start_sync("managed_identity")
        with self.database.connect() as db:
            db.execute(
                """
                UPDATE sync_runs
                SET status = 'running',
                    started_at = started_at - INTERVAL '2 hours',
                    claimed_at = started_at - INTERVAL '2 hours'
                WHERE id = ?
                """,
                [sync_id],
            )

        health = self.database.operational_health()

        self.assertEqual(health["status"], "critical")
        self.assertEqual(health["worker"]["status"], "stalled")

    def test_integration_reads_persisted_timestamp(self):
        integration = self.database.integration()

        self.assertEqual(integration["name"], "Azure")
        self.assertEqual(integration["authMode"], "local_powershell")
        self.assertIsNotNone(integration["updatedAt"])
        self.assertTrue(
            all(
                source["nextExpectedAt"]
                for source in integration["sourceFreshness"]
            )
        )

    def test_sync_requests_are_persisted_and_claimed(self):
        sync_id = self.database.start_sync("managed_identity")

        queued = self.database.latest_sync()
        self.assertEqual(queued["id"], sync_id)
        self.assertEqual(queued["status"], "queued")
        self.assertIsNone(queued["claimedAt"])
        self.assertEqual(self.database.active_sync()["id"], sync_id)

        claimed = self.database.claim_next_sync()
        self.assertEqual(claimed["id"], sync_id)
        self.assertFalse(claimed["recovered"])
        running = self.database.latest_sync()
        self.assertEqual(running["status"], "running")
        self.assertIsNotNone(running["claimedAt"])

    def test_sync_worker_recovers_an_orphaned_running_request(self):
        sync_id = self.database.start_sync("managed_identity")
        self.database.claim_next_sync()

        # A live claim holds an unexpired lease and must not be stolen.
        self.assertIsNone(self.database.claim_next_sync())

        # Once the lease lapses (crashed worker), the sync is recoverable.
        with self.database.operational_connect() as db:
            db.execute(
                "UPDATE sync_runs SET claim_expires_at = ? WHERE id = ?",
                [datetime(2000, 1, 1, tzinfo=timezone.utc), sync_id],
            )
        recovered = self.database.claim_next_sync()

        self.assertEqual(recovered["id"], sync_id)
        self.assertTrue(recovered["recovered"])
        self.assertIn(
            "worker restart",
            self.database.latest_sync()["stageMessage"],
        )

    def test_source_specific_request_and_scope_checkpoint_are_persisted(self):
        sync_id = self.database.start_sync(
            "managed_identity",
            trigger="scheduled",
            sources=["cost"],
        )
        claimed = self.database.claim_next_sync()
        self.assertEqual(claimed["sources"], ["cost"])

        self.database.begin_sync_source(sync_id, "ActualCost", "sub-1")
        self.database.finish_sync_source(
            sync_id,
            "ActualCost",
            "sub-1",
            "succeeded",
            12,
            "Collected scope.",
        )

        latest = self.database.latest_sync()
        self.assertEqual(latest["requestedSources"], ["cost"])
        self.assertEqual(latest["sourceRuns"][0]["rowCount"], 12)
        self.assertTrue(
            self.database.sync_source_completed(sync_id, "ActualCost", "sub-1")
        )

    def test_current_cost_scope_order_prioritizes_missing_commitment(self):
        self.database.store_snapshot(
            "cost-current",
            [],
            inventory_collected=False,
            costs=[
                {
                    "periodStart": "2026-07-01",
                    "periodEnd": "2026-07-26",
                    "costType": "ActualCost",
                    "subscriptionId": "sub-1",
                    "resourceId": "/subscriptions/sub-1",
                    "amount": 12.0,
                    "currency": "USD",
                    "source": "azure_cost_management_query",
                }
            ],
            cost_scopes=[("sub-1", "ActualCost")],
        )

        ordered = self.database.cost_sync_scope_order(
            [
                ("sub-1", "One", "ActualCost"),
                ("sub-2", "Two", "AmortizedCost"),
                ("sub-1", "One", "CommitmentCoverage"),
            ]
        )

        self.assertEqual(ordered[0][2], "CommitmentCoverage")
        self.assertEqual(ordered[-1][2], "ActualCost")

    def test_cost_reconciliation_discloses_independent_scope_coverage(self):
        integration = self.database.integration()
        self.database.save_integration(
            {
                "name": integration["name"],
                "tenantId": integration["tenantId"],
                "enabled": True,
                "authMode": integration["authMode"],
                "subscriptions": [
                    {"subscriptionId": "sub-1", "label": "Production"},
                    {"subscriptionId": "sub-2", "label": "Development"},
                ],
            }
        )
        today = utc_now().date()  # the app reasons in UTC; local date flakes at month ends
        self.database.store_snapshot(
            "current-cost",
            [],
            inventory_collected=False,
            costs=[
                {
                    "periodStart": today.replace(day=1).isoformat(),
                    "periodEnd": today.isoformat(),
                    "costType": "ActualCost",
                    "subscriptionId": "sub-1",
                    "resourceId": "/subscriptions/sub-1",
                    "amount": 125.0,
                    "currency": "USD",
                    "source": "azure_cost_management_query",
                }
            ],
            cost_scopes=[("sub-1", "ActualCost")],
        )
        self.database.store_daily_cost_scope(
            "daily-cost",
            "sub-2",
            "ActualCost",
            [
                {
                    "usageDate": today.isoformat(),
                    "resourceId": "/subscriptions/sub-2",
                    "serviceName": "Compute",
                    "amount": 10.0,
                    "currency": "USD",
                }
            ],
            start_date=today,
            end_date=today,
        )

        status = self.database.cost_reconciliation()
        by_source = {item["source"]: item for item in status["datasets"]}

        self.assertEqual(status["configuredSubscriptions"], 2)
        self.assertEqual(by_source["ActualCost"]["availableScopes"], 1)
        self.assertEqual(by_source["ActualCost"]["currentPeriodScopes"], 1)
        self.assertEqual(by_source["ActualCost"]["amount"], 125.0)
        self.assertFalse(by_source["ActualCost"]["complete"])
        self.assertEqual(by_source["DailyActualCost"]["availableScopes"], 1)
        self.assertEqual(
            by_source["ActualCost"]["scopes"][0]["subscriptionName"],
            "Production",
        )
        self.assertIsNone(
            by_source["ActualCost"]["scopes"][1]["lastSuccessfulAt"]
        )

    def test_failed_source_attempt_retains_last_good_snapshot(self):
        resource_id = (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "microsoft.compute/virtualmachines/vm-1"
        )
        self.database.store_snapshot(
            "good",
            [],
            inventory_collected=False,
            advisor=[
                {
                    "recommendationId": "advisor-1",
                    "resourceId": resource_id,
                    "subscriptionId": "sub",
                    "subscriptionName": "Production",
                    "resourceType": "microsoft.compute/virtualmachines",
                    "category": "Cost",
                    "impact": "High",
                    "problem": "Underutilized.",
                    "solution": "Resize.",
                    "savingsAmount": 10.0,
                    "savingsCurrency": "USD",
                    "raw": {},
                }
            ],
            advisor_collected=True,
        )
        sync_id = self.database.start_sync(
            "managed_identity", sources=["advisor"]
        )
        self.database.begin_sync_source(
            sync_id, "AzureAdvisor", "configured-subscriptions"
        )
        self.database.finish_sync_source(
            sync_id,
            "AzureAdvisor",
            "configured-subscriptions",
            "failed",
            0,
            "Timed out.",
            retained_last_good=True,
        )

        self.assertEqual(
            self.database.opportunities(source="azure_advisor")["total"],
            1,
        )
        advisor = next(
            item
            for item in self.database.source_freshness()
            if item["source"] == "AzureAdvisor"
        )
        self.assertTrue(advisor["retainedLastGood"])
        self.assertEqual(advisor["lastAttemptStatus"], "failed")

    def test_snapshot_drives_inventory_and_opportunities(self):
        self.database.store_snapshot(
            "snapshot-1",
            [
                {
                    "resourceId": "/subscriptions/sub/resourceGroups/rg/providers/microsoft.compute/disks/disk-1",
                    "name": "disk-1",
                    "resourceType": "microsoft.compute/disks",
                    "subscriptionId": "sub",
                    "subscriptionName": "Production",
                    "resourceGroup": "rg",
                    "region": "eastus2",
                    "kind": "",
                    "sku": "Premium_LRS",
                    "provisioningState": "Succeeded",
                    "managedBy": "",
                    "tags": {"owner": "platform"},
                    "estimatedMonthlyCost": 100.0,
                    "costSource": "test",
                    "utilizationPercent": None,
                    "utilizationSource": None,
                    "opportunityKind": "unattached_disk",
                    "opportunityReason": "No owner.",
                    "estimatedMonthlySavings": 100.0,
                    "raw": {},
                }
            ],
        )
        inventory = self.database.inventory()
        opportunities = self.database.inventory(opportunity_only=True)
        overview = self.database.overview()

        self.assertEqual(inventory["total"], 1)
        self.assertEqual(opportunities["items"][0]["opportunityKind"], "unattached_disk")
        self.assertEqual(overview["summary"]["estimatedMonthlyCost"], 100.0)
        self.assertEqual(overview["summary"]["opportunityCount"], 0)

    def test_current_inventory_uses_latest_complete_snapshot(self):
        first = [
            {
                "resourceId": "/subscriptions/sub/resourceGroups/rg/providers/test/type/old",
                "name": "old",
                "resourceType": "test/type",
                "subscriptionId": "sub",
            },
            {
                "resourceId": "/subscriptions/sub/resourceGroups/rg/providers/test/type/current",
                "name": "current",
                "resourceType": "test/type",
                "subscriptionId": "sub",
            },
        ]
        self.database.store_snapshot("first", first)
        self.database.store_snapshot("second", [first[1]])
        inventory = self.database.inventory()
        self.assertEqual(inventory["total"], 1)
        self.assertEqual(inventory["items"][0]["name"], "current")

    def test_inventory_pagination_retains_filtered_total(self):
        self.database.store_snapshot(
            "paged-inventory",
            [
                {
                    "resourceId": f"/subscriptions/sub/providers/test/type/{name}",
                    "name": name,
                    "resourceType": "test/type",
                    "subscriptionId": "sub",
                    "subscriptionName": "Production",
                }
                for name in ("alpha", "bravo", "charlie")
            ],
        )

        first = self.database.inventory(
            resource_type="test/type",
            limit=2,
            offset=0,
        )
        second = self.database.inventory(
            resource_type="test/type",
            limit=2,
            offset=2,
        )

        self.assertEqual(first["total"], 3)
        self.assertEqual([item["name"] for item in first["items"]], ["alpha", "bravo"])
        self.assertEqual(second["total"], 3)
        self.assertEqual([item["name"] for item in second["items"]], ["charlie"])

    def test_subscription_scoped_advisor_has_honest_display_name(self):
        self.database.store_snapshot(
            "subscription-advisor",
            [
                {
                    "resourceId": "/subscriptions/sub/resourceGroups/rg/providers/microsoft.compute/virtualmachines/vm-1",
                    "name": "vm-1",
                    "resourceType": "microsoft.compute/virtualmachines",
                    "subscriptionId": "sub",
                    "subscriptionName": "Production",
                }
            ],
            advisor=[
                {
                    "recommendationId": "reservation-1",
                    "resourceId": "/subscriptions/sub",
                    "subscriptionId": "sub",
                    "subscriptionName": "Production",
                    "resourceType": "microsoft.compute/virtualmachines",
                    "category": "Cost",
                    "impact": "High",
                    "problem": "Consider virtual machine reserved instances.",
                    "solution": "Review reservation coverage.",
                    "savingsAmount": 100.0,
                    "savingsCurrency": "USD",
                    "raw": {
                        "_fluxScopeType": "subscription",
                        "_fluxActionContext": (
                            "SKU Standard_D4s_v5 · region westus3 · term P3Y"
                        ),
                    },
                },
                {
                    # Advisor can emit a new ID for the same actionable
                    # reservation variant. The opportunity layer must retain
                    # one semantic action, not make the label look repeated.
                    "recommendationId": "reservation-duplicate-variant",
                    "resourceId": "/subscriptions/sub",
                    "subscriptionId": "sub",
                    "subscriptionName": "Production",
                    "resourceType": "microsoft.compute/virtualmachines",
                    "category": "Cost",
                    "impact": "High",
                    "problem": "Consider virtual machine reserved instances.",
                    "solution": "Review reservation coverage.",
                    "savingsAmount": 90.0,
                    "savingsCurrency": "USD",
                    "raw": {
                        "_fluxScopeType": "subscription",
                        "_fluxActionContext": (
                            "SKU Standard_D4s_v5 · region westus3 · term P3Y"
                        ),
                    },
                },
            ],
            advisor_collected=True,
        )

        opportunities = self.database.opportunities(source="azure_advisor")
        self.assertEqual(opportunities["total"], 1)
        item = opportunities["items"][0]
        self.assertEqual(item["resourceName"], "Production subscription scope")
        self.assertIn("Review reservation coverage", item["title"])
        self.assertIn("Standard_D4s_v5", item["title"])
        self.assertIn("Scope details", item["reason"])
        self.assertEqual(item["resourceId"], "/subscriptions/sub")
        self.assertEqual(item["actionability"], "portfolio_review")
        self.assertEqual(
            self.database.opportunities(
                source="azure_advisor",
                actionability="actionable_now",
            )["total"],
            0,
        )
        self.assertEqual(
            opportunities["summary"]["portfolio"]["portfolioReview"],
            1,
        )
        self.assertEqual(
            self.database.opportunities(source="azure_advisor")[
                "diagnostics"
            ]["sourceRows"][0]["duplicates"],
            0,
        )

    def test_default_opportunities_hide_non_finops_advisor_categories(self):
        base = {
            "resourceId": "/subscriptions/sub",
            "subscriptionId": "sub",
            "subscriptionName": "Production",
            "resourceType": "",
            "impact": "High",
            "problem": "Review this recommendation.",
            "solution": "Take the documented action.",
            "savingsCurrency": "USD",
            "raw": {},
        }
        self.database.store_snapshot(
            "advisor-categories",
            [],
            advisor=[
                {
                    **base,
                    "recommendationId": "cost-1",
                    "category": "Cost",
                    "savingsAmount": 10.0,
                },
                {
                    **base,
                    "recommendationId": "security-1",
                    "category": "Security",
                    "savingsAmount": None,
                },
            ],
            advisor_collected=True,
        )

        self.assertEqual(
            self.database.opportunities(source="azure_advisor")["total"], 1
        )
        self.assertEqual(
            self.database.opportunities(
                source="azure_advisor",
                category="Security",
            )["total"],
            1,
        )
        overview = self.database.overview()
        self.assertEqual(
            next(
                item["value"]
                for item in overview["opportunitiesBySource"]
                if item["name"] == "Azure Advisor"
            ),
            1,
        )

    def test_cost_and_advisor_enrichment_drive_current_views(self):
        resource_id = (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "microsoft.compute/virtualmachines/vm-1"
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO finops_toolkit_commitment_eligibility
                VALUES
                    ('v14', now(), 'meter-covered', 'Eligible', 'Eligible'),
                    ('v14', now(), 'meter-ondemand', 'Eligible', 'Eligible')
                """
            )
        self.database.store_snapshot(
            "snapshot-enriched",
            [
                {
                    "resourceId": resource_id,
                    "name": "vm-1",
                    "resourceType": "microsoft.compute/virtualmachines",
                    "subscriptionId": "sub",
                    "subscriptionName": "Production",
                    "resourceGroup": "rg",
                    "region": "eastus2",
                    "kind": "",
                    "sku": "Standard_D4s_v5",
                    "provisioningState": "Succeeded",
                    "managedBy": "",
                    "tags": {"owner": "platform"},
                    "estimatedMonthlyCost": None,
                    "costSource": None,
                    "utilizationPercent": None,
                    "utilizationSource": None,
                    "opportunityKind": None,
                    "opportunityReason": None,
                    "estimatedMonthlySavings": None,
                    "raw": {},
                }
            ],
            costs=[
                {
                    "periodStart": "2026-07-01",
                    "periodEnd": "2026-07-24",
                    "costType": "ActualCost",
                    "subscriptionId": "sub",
                    "resourceId": resource_id,
                    "amount": 40.0,
                    "currency": "USD",
                    "source": "azure_cost_management_query",
                },
                {
                    "periodStart": "2026-07-01",
                    "periodEnd": "2026-07-24",
                    "costType": "AmortizedCost",
                    "subscriptionId": "sub",
                    "resourceId": resource_id,
                    "amount": 35.0,
                    "currency": "USD",
                    "source": "azure_cost_management_query",
                },
            ],
            cost_scopes=[
                ("sub", "ActualCost"),
                ("sub", "AmortizedCost"),
            ],
            commitment_costs=[
                {
                    "periodStart": "2026-07-01",
                    "periodEnd": "2026-07-24",
                    "subscriptionId": "sub",
                    "meterId": "meter-covered",
                    "pricingModel": "Reservation",
                    "amount": 30.0,
                    "currency": "USD",
                },
                {
                    "periodStart": "2026-07-01",
                    "periodEnd": "2026-07-24",
                    "subscriptionId": "sub",
                    "meterId": "meter-ondemand",
                    "pricingModel": "OnDemand",
                    "amount": 70.0,
                    "currency": "USD",
                },
            ],
            commitment_scopes=["sub"],
            advisor=[
                {
                    "recommendationId": "advisor-1",
                    "resourceId": resource_id,
                    "subscriptionId": "sub",
                    "subscriptionName": "Production",
                    "resourceType": "microsoft.compute/virtualmachines",
                    "category": "Cost",
                    "impact": "High",
                    "problem": "The VM is underutilized.",
                    "solution": "Resize the VM.",
                    "savingsAmount": 15.0,
                    "annualSavingsAmount": 180.0,
                    "savingsCurrency": "USD",
                    "recommendationTypeId": "advisor-type-1",
                    "currentSku": "Standard_D4s_v5",
                    "recommendedSku": "Standard_D2s_v5",
                    "lastUpdated": "2026-07-24T10:00:00Z",
                    "learnMoreLink": "https://learn.microsoft.com/",
                    "raw": {},
                }
            ],
            advisor_collected=True,
            intelligence=[
                {
                    "findingId": f"stopped_allocated_vm:{resource_id}",
                    "ruleId": "stopped_allocated_vm",
                    "source": "flux_intelligence",
                    "resourceId": resource_id,
                    "relatedResourceId": "",
                    "subscriptionId": "sub",
                    "subscriptionName": "Production",
                    "resourceType": "microsoft.compute/virtualmachines",
                    "resourceGroup": "rg",
                    "region": "eastus2",
                    "category": "Cost",
                    "impact": "High",
                    "confidence": "High",
                    "title": "vm-1: Stopped but allocated VM",
                    "reason": "The VM is still billed.",
                    "evidence": {"powerState": "PowerState/stopped"},
                    "estimatedMonthlySavings": None,
                    "savingsCurrency": "",
                    "ruleVersion": "test",
                },
                {
                    "findingId": f"missing_allocation_tags:{resource_id}",
                    "ruleId": "missing_allocation_tags",
                    "source": "flux_intelligence",
                    "resourceId": resource_id,
                    "relatedResourceId": "",
                    "subscriptionId": "sub",
                    "subscriptionName": "Production",
                    "resourceType": "microsoft.compute/virtualmachines",
                    "resourceGroup": "rg",
                    "region": "eastus2",
                    "category": "Governance",
                    "impact": "Low",
                    "confidence": "Review",
                    "title": "vm-1: Missing allocation tags",
                    "reason": "The VM has no allocation tags.",
                    "evidence": {},
                    "estimatedMonthlySavings": None,
                    "savingsCurrency": "",
                    "ruleVersion": "test",
                },
            ],
            intelligence_collected=True,
        )

        inventory = self.database.inventory()
        opportunities = self.database.opportunities(source="azure_advisor")
        overview = self.database.overview()

        self.assertEqual(inventory["items"][0]["estimatedMonthlyCost"], 40.0)
        self.assertEqual(inventory["items"][0]["amortizedMonthlyCost"], 35.0)
        self.assertEqual(inventory["items"][0]["costCurrency"], "USD")
        self.assertEqual(opportunities["total"], 1)
        self.assertEqual(opportunities["items"][0]["source"], "azure_advisor")
        self.assertEqual(
            opportunities["items"][0]["estimatedMonthlySavings"],
            15.0,
        )
        self.assertEqual(opportunities["items"][0]["annualSavingsAmount"], 180.0)
        self.assertEqual(
            opportunities["items"][0]["recommendedSku"],
            "Standard_D2s_v5",
        )
        intelligence = self.database.opportunities(
            source="flux_intelligence",
            include_governance=True,
        )
        self.assertEqual(intelligence["total"], 2)
        self.assertEqual(
            {item["confidence"] for item in intelligence["items"]},
            {"High", "Review"},
        )
        self.assertEqual(intelligence["items"][0]["actualMonthlyCost"], 40.0)
        focused = self.database.opportunities(source="flux_intelligence", limit=1)
        self.assertEqual(focused["total"], 1)
        self.assertEqual(len(focused["items"]), 1)
        self.assertEqual(focused["summary"]["costExposure"], 40.0)
        self.assertNotIn("Governance", {item["category"] for item in focused["items"]})
        actionable = self.database.opportunities(
            actionability="actionable_now",
        )
        self.assertTrue(actionable["items"])
        self.assertEqual(
            {item["actionability"] for item in actionable["items"]},
            {"actionable_now"},
        )
        self.assertGreaterEqual(len(self.database.source_freshness()), 4)
        self.assertEqual(overview["summary"]["estimatedMonthlyCost"], 40.0)
        self.assertEqual(overview["summary"]["opportunityCount"], 0)
        self.assertEqual(overview["summary"]["estimatedMonthlySavings"], 0.0)
        self.assertEqual(
            overview["costBySubscription"],
            [{
                "name": "Production",
                "actual": 40.0,
                "amortized": 35.0,
                "subscriptionId": "sub",
            }],
        )
        self.assertEqual(overview["commitmentCoverage"]["status"], "ready")
        self.assertEqual(
            overview["commitmentCoverage"]["coveragePercent"],
            30.0,
        )
        self.assertEqual(
            overview["commitmentCoverage"]["eligibleOnDemandCost"],
            70.0,
        )
        self.assertEqual(
            overview["commitmentCostMix"],
            [
                {"name": "On-demand", "value": 70.0},
                {"name": "Reservation", "value": 30.0},
            ],
        )
        self.assertEqual(
            overview["opportunitiesBySource"],
            [
                {"name": "Inventory rules", "value": 0},
                {"name": "Azure Advisor", "value": 1},
                {"name": "Flux Signals", "value": 2},
            ],
        )


if __name__ == "__main__":
    unittest.main()


class PipelineStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "test.duckdb")
        self.database.init()

    def tearDown(self):
        self.temp.cleanup()

    def test_pipeline_status_reports_queue_applies_and_throttles(self):
        sync_id = self.database.start_sync("managed_identity")
        self.database.claim_throttle_slot("cost-management", 60)
        from api.analytics_writer import stage_payload

        stage_payload(
            self.database,
            Path(self.temp.name) / "staging",
            "retail-prices",
            "pipeline-test-key",
            {"snapshotId": "s", "prices": [], "complete": True},
        )

        status = self.database.pipeline_status()

        self.assertEqual(status["syncs"]["queuedCount"], 1)
        self.assertEqual(status["syncs"]["pending"][0]["id"], sync_id)
        self.assertGreaterEqual(
            status["syncs"]["pending"][0]["queuedAgeSeconds"], 0
        )
        self.assertIsNone(status["publication"]["version"])
        self.assertEqual(status["applyJobs"]["stagedCount"], 1)
        self.assertEqual(status["applyJobs"]["failedCount"], 0)
        names = [item["name"] for item in status["throttles"]]
        self.assertIn("cost-management", names)
        self.assertGreater(status["throttles"][0]["blockedForSeconds"], 0)

    def test_pipeline_status_shows_claimed_sync_and_publication(self):
        self.database.start_sync("managed_identity")
        claimed = self.database.claim_next_sync()
        self.database.record_analytics_publication(
            status="approved",
            file_name="flux-analytics-00000001.duckdb",
            checksum="abc",
            file_size_bytes=123,
            row_counts={"resources_current": 1},
        )

        status = self.database.pipeline_status()

        self.assertEqual(status["syncs"]["runningCount"], 1)
        running = status["syncs"]["pending"][0]
        self.assertEqual(running["id"], claimed["id"])
        self.assertEqual(running["claimedBy"], self.database.worker_id)
        self.assertIsNotNone(running["claimExpiresAt"])
        self.assertEqual(status["publication"]["version"], 1)
        self.assertGreaterEqual(status["publication"]["ageSeconds"], 0)


class PipelineWarningTests(unittest.TestCase):
    """An unclaimed queue must be reported, not merely observable."""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "warn.duckdb")
        self.database.init()

    def tearDown(self):
        self.temp.cleanup()

    def test_healthy_pipeline_has_no_warnings(self):
        self.assertEqual(self.database.pipeline_status()["warnings"], [])

    def test_recently_queued_sync_is_not_yet_a_warning(self):
        self.database.start_sync("managed_identity")
        self.assertEqual(self.database.pipeline_status()["warnings"], [])

    def test_long_unclaimed_queue_is_reported(self):
        sync_id = self.database.start_sync("managed_identity")
        with self.database.operational_connect() as db:
            db.execute(
                "UPDATE sync_runs SET started_at = ? WHERE id = ?",
                [datetime(2020, 1, 1, tzinfo=timezone.utc), sync_id],
            )
        warnings = self.database.pipeline_status()["warnings"]
        self.assertTrue(warnings, "an hours-old unclaimed queue must warn")
        self.assertIn("unclaimed", warnings[0])
        self.assertIn("no worker is consuming the queue", warnings[0])

    def test_claimed_sync_does_not_warn_as_unclaimed(self):
        sync_id = self.database.start_sync("managed_identity")
        with self.database.operational_connect() as db:
            db.execute(
                "UPDATE sync_runs SET started_at = ? WHERE id = ?",
                [datetime(2020, 1, 1, tzinfo=timezone.utc), sync_id],
            )
        self.database.claim_next_sync()
        warnings = self.database.pipeline_status()["warnings"]
        self.assertFalse(
            [w for w in warnings if "unclaimed" in w],
            "a claimed sync is being worked and must not warn as unclaimed",
        )

    def test_history_prune_never_changes_what_current_resolves_to(self):
        """Retention removes superseded history only.

        A stalled source's last-good snapshot survives regardless of age, and
        ranked scoring keeps the newest row per opportunity even when that
        row is itself older than the cutoff.
        """
        now = datetime.now(timezone.utc)

        def days_ago(days):
            return now - timedelta(days=days)

        with self.database.connect() as db:
            # Stalled source: latest snapshot 90 days old, superseded one 95.
            db.execute(
                "INSERT INTO source_sync_state VALUES "
                "('snap-old', ?, 'ActualCost', 'sub-1', 1),"
                "('snap-latest', ?, 'ActualCost', 'sub-1', 1)",
                [days_ago(95), days_ago(90)],
            )
            db.execute(
                "INSERT INTO cost_snapshots VALUES "
                "('snap-old', ?, DATE '2026-04-01', DATE '2026-04-30',"
                " 'ActualCost', 'sub-1', '/r/1', 10, 'USD', 'test'),"
                "('snap-latest', ?, DATE '2026-05-01', DATE '2026-05-31',"
                " 'ActualCost', 'sub-1', '/r/1', 20, 'USD', 'test')",
                [days_ago(95), days_ago(90)],
            )
            # Ranked scoring: superseded old row plus a newest row that is
            # itself past the cutoff.
            db.execute(
                "INSERT INTO opportunity_confidence_snapshots VALUES "
                "('c-old', ?, '/r/1', 'idle', ?, ?, 1, FALSE, 0.5, 'Medium',"
                " '{}', 'v1'),"
                "('c-new', ?, '/r/1', 'idle', ?, ?, 2, FALSE, 0.6, 'Medium',"
                " '{}', 'v1')",
                [
                    days_ago(90), days_ago(120), days_ago(90),
                    days_ago(70), days_ago(120), days_ago(70),
                ],
            )
            db.execute(
                "INSERT INTO telemetry_metric_samples VALUES "
                "('t1', ?, 'azure_monitor', 'src-1', '/r/1',"
                " 'cpu', 'percent', ?, 12.5, '{}'),"
                "('t2', ?, 'azure_monitor', 'src-1', '/r/1',"
                " 'cpu', 'percent', ?, 13.5, '{}')",
                [days_ago(100), days_ago(100), days_ago(10), days_ago(10)],
            )

        deleted = self.database.prune_analytics_history(
            history_days=60, telemetry_sample_days=45
        )
        self.assertEqual(deleted["cost_snapshots"], 1)
        self.assertEqual(deleted["opportunity_confidence_snapshots"], 1)
        self.assertEqual(deleted["telemetry_metric_samples"], 1)

        with self.database.connect(read_only=True) as db:
            kept_cost = [
                row[0] for row in db.execute(
                    "SELECT snapshot_id FROM cost_snapshots"
                ).fetchall()
            ]
            kept_confidence = [
                row[0] for row in db.execute(
                    "SELECT snapshot_id FROM opportunity_confidence_snapshots"
                ).fetchall()
            ]
            kept_samples = db.execute(
                "SELECT count(*) FROM telemetry_metric_samples"
            ).fetchone()[0]
        self.assertEqual(kept_cost, ["snap-latest"])
        self.assertEqual(kept_confidence, ["c-new"])
        self.assertEqual(kept_samples, 1)

    def test_stale_triggered_job_lock_is_reported(self):
        """A lock whose heartbeat outlived its process must warn.

        Third orphaned-lock variant, 2026-08-01: flux-cost-history died in a
        deploy restart, Kudu kept reporting it Running, and every scheduled
        run was silently skipped for two days.
        """
        import os
        import time as time_module

        jobs_root = Path(self.temp.name) / "jobs"
        stale_dir = jobs_root / "triggered" / "flux-cost-history"
        stale_dir.mkdir(parents=True)
        (stale_dir / "triggeredJob.lock").touch()
        heartbeat = stale_dir / "triggeredJob.lock.heartbeat"
        heartbeat.touch()
        old = time_module.time() - 3600
        os.utime(heartbeat, (old, old))

        fresh_dir = jobs_root / "triggered" / "flux-advisor"
        fresh_dir.mkdir(parents=True)
        (fresh_dir / "triggeredJob.lock").touch()
        (fresh_dir / "triggeredJob.lock.heartbeat").touch()

        clean_dir = jobs_root / "triggered" / "flux-daily"
        clean_dir.mkdir(parents=True)

        with patch.dict(
            os.environ, {"FLUX_WEBJOBS_DATA_ROOT": str(jobs_root)}
        ):
            warnings = self.database.pipeline_status()["warnings"]
        stale_warnings = [w for w in warnings if "flux-cost-history" in w]
        self.assertTrue(
            stale_warnings, "an hour-stale triggered-job lock must warn"
        )
        self.assertIn("silently skipping", stale_warnings[0])
        self.assertFalse(
            [w for w in warnings if "flux-advisor" in w],
            "a fresh heartbeat is a legitimately running job",
        )
        self.assertFalse(
            [w for w in warnings if "flux-daily" in w],
            "no lock file means nothing to warn about",
        )

    def _seed_change(self, snapshot_id: str, days_ago: int, name: str) -> None:
        """Insert one inventory change row aged `days_ago` days."""
        computed_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
        with self.database.connect() as db:
            db.execute(
                """
                INSERT INTO inventory_drift_runs
                VALUES (?, '', ?, 1, 'v1')
                ON CONFLICT DO NOTHING
                """,
                [snapshot_id, computed_at],
            )
            db.execute(
                """
                INSERT INTO inventory_changes VALUES (
                    ?, '', ?, ?, ?, 'vm', 'sub-1', 'Sub One', 'rg-1',
                    'eastus', 'added', '', '', '{}', 'v1'
                )
                """,
                [snapshot_id, computed_at, f"/id/{name}", name],
            )

    def test_changes_window_days_filters_to_the_requested_period(self):
        # One drift snapshot so inventory_changes_current resolves them all.
        self._seed_change("snap-1", 0, "today")
        self._seed_change("snap-1", 3, "three-days")
        self._seed_change("snap-1", 20, "twenty-days")

        recent = self.database.changes(window_days=7)
        self.assertEqual(recent["total"], 2)
        self.assertEqual(
            {item["resourceName"] for item in recent["items"]},
            {"today", "three-days"},
        )

        everything = self.database.changes(window_days=0)
        self.assertEqual(
            everything["total"], 3, "window_days=0 must mean all history"
        )

        one_day = self.database.changes(window_days=1)
        self.assertEqual({i["resourceName"] for i in one_day["items"]}, {"today"})

    def test_cost_anomaly_trend_excludes_unfinalized_recent_days(self):
        # The trend query compares against DuckDB's current_date (session
        # clock), so the seed must use the same clock or the newest row
        # lands "in the future" during the UTC/local month-end window.
        with self.database.connect(read_only=True) as db:
            today = db.execute("SELECT current_date").fetchone()[0]
        with self.database.connect() as db:
            for offset, amount in ((0, 5.0), (1, 6.0), (3, 100.0), (5, 110.0)):
                db.execute(
                    """
                    INSERT INTO daily_cost_history VALUES (
                        'snap-1', ?, ?, 'AmortizedCost', 'sub-1',
                        '/id/res-1', 'Compute', ?, 'USD', 'test'
                    )
                    """,
                    [
                        datetime.now(timezone.utc),
                        today - timedelta(days=offset),
                        amount,
                    ],
                )

        # Cost Management has not finalized the newest days; charting them
        # renders a false cliff, so they must be cut from the series.
        lagged = self.database.cost_anomalies(latency_days=2)
        dates = {point["date"] for point in lagged["trend"]}
        self.assertNotIn(today.isoformat(), dates)
        self.assertNotIn((today - timedelta(days=1)).isoformat(), dates)
        self.assertIn((today - timedelta(days=3)).isoformat(), dates)
        self.assertEqual(lagged["trendLatencyDays"], 2)

        # latency_days=0 keeps the historical behaviour of showing everything.
        raw = self.database.cost_anomalies(latency_days=0)
        self.assertIn(today.isoformat(), {p["date"] for p in raw["trend"]})

    def test_connect_takes_the_cross_instance_writer_lease(self):
        """The file lock is client-local on the CIFS /home mount, so every
        mutable open must also take the operational-store advisory lease.
        Without this the two App Service instances write concurrently, which
        is what corrupted the database repeatedly through 2026-07-30."""
        taken: list[float] = []
        real = self.database._operational.duckdb_writer_lease

        @contextmanager
        def spy(timeout: float = -1.0):
            taken.append(timeout)
            with real(timeout=timeout):
                yield

        with patch.object(
            self.database._operational, "duckdb_writer_lease", spy
        ):
            with self.database.connect() as db:
                db.execute("SELECT 1").fetchone()
        self.assertEqual(len(taken), 1, "mutable connect must take the global lease")

    def test_snapshot_reads_skip_the_global_lease(self):
        """Immutable snapshot reads are the hot path and must stay lock-free."""
        publication = Path(self.temp.name) / "snap.duckdb"
        import duckdb as _duckdb

        _duckdb.connect(str(publication)).close()
        self.database.attach_read_snapshot(publication)
        taken: list[float] = []

        @contextmanager
        def spy(timeout: float = -1.0):
            taken.append(timeout)
            yield

        with patch.object(
            self.database._operational, "duckdb_writer_lease", spy
        ):
            with self.database.connect(read_only=True) as db:
                db.execute("SELECT 1").fetchone()
        self.assertEqual(taken, [], "snapshot reads must not contend for the lease")

    def test_nested_connect_does_not_deadlock_on_its_own_lease(self):
        """The advisory lock is taken on a fresh pooled connection, so a
        nested open must reuse the lease rather than wait for itself."""
        with self.database.connect() as outer:
            outer.execute("SELECT 1").fetchone()
            with self.database.writer_lease(timeout=5):
                self.assertGreater(self.database._writer_lease_depth, 0)
        self.assertEqual(self.database._writer_lease_depth, 0)
