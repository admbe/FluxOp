from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase
from api.forecasting import forecast_daily_cost


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "reporting.duckdb")
        self.database.init()

    def tearDown(self):
        self.temp.cleanup()

    def test_weekday_forecast_requires_history_and_publishes_bounds(self):
        warming = forecast_daily_cost({})
        self.assertEqual(warming["status"], "not_connected")

        start = date(2026, 4, 1)
        history = {
            start + timedelta(days=index): 100 + (index % 7) * 5
            for index in range(100)
        }
        forecast = forecast_daily_cost(history)
        self.assertEqual(forecast["status"], "ready")
        self.assertEqual(len(forecast["points"]), 30)
        self.assertGreaterEqual(forecast["upperTotal"], forecast["forecastTotal"])
        self.assertLessEqual(forecast["lowerTotal"], forecast["forecastTotal"])

    def test_cost_report_is_currency_safe_and_forecast_ready(self):
        self.database.save_integration(
            {
                "name": "Azure",
                "tenantId": "tenant",
                "enabled": True,
                "authMode": "managed_identity",
                "subscriptions": [
                    {"subscriptionId": "sub-1", "label": "Production"},
                    {"subscriptionId": "sub-2", "label": "Development"},
                ],
            }
        )
        start = date(2026, 4, 1)
        records = []
        for index in range(100):
            usage_date = start + timedelta(days=index)
            records.append(
                {
                    "usageDate": usage_date.isoformat(),
                    "costType": "AmortizedCost",
                    "subscriptionId": "sub-1",
                    "resourceId": "/subscriptions/sub-1/resources/vm-1",
                    "serviceName": "Virtual Machines",
                    "amount": 100 + index % 7,
                    "currency": "USD",
                    "source": "test",
                }
            )
        self.database.store_daily_cost_scope(
            "history-1",
            "sub-1",
            "AmortizedCost",
            records,
            start_date=start,
            end_date=start + timedelta(days=99),
        )
        report = self.database.cost_report(cost_type="AmortizedCost")
        self.assertEqual(report["summary"]["currency"], "USD")
        self.assertEqual(len(report["daily"]), 30)
        self.assertEqual(report["forecast"]["status"], "ready")
        self.assertEqual(report["byService"][0]["name"], "Virtual Machines")
        self.assertEqual(
            report["costTypeComparison"]["AmortizedCost"],
            report["summary"]["totalCost"],
        )
        self.assertTrue(report["topMovers"]["resources"])
        self.assertTrue(report["forecast"]["monthly"])
        self.assertEqual(report["forecast"]["latencyDays"], 2)
        self.assertFalse(report["dataCoverage"]["complete"])
        self.assertEqual(report["dataCoverage"]["configuredScopes"], 2)
        self.assertEqual(report["dataCoverage"]["availableScopes"], 1)
        missing = next(
            item
            for item in report["dataCoverage"]["scopes"]
            if item["id"] == "sub-2"
        )
        self.assertEqual(missing["status"], "not_collected")
        self.assertIn(
            {"id": "sub-2", "name": "Development"},
            report["facets"]["subscriptions"],
        )

    def test_cost_report_defaults_to_finalized_horizon(self):
        """A first-of-month start must not invert or silently extend.

        Live incident 2026-08-01: startDate on the first of the month with
        the end clamped to the finalized horizon (max ingested day minus the
        latency window) produced start > end, returning 0-vs-0 totals and a
        previous window in the future. The contract now matches the rest of
        Flux reporting: as-of the finalized horizon by default, with
        unfinalized rows served only on an explicit endDate opt-in.
        """
        self.database.save_integration(
            {
                "name": "Azure",
                "tenantId": "tenant",
                "enabled": True,
                "authMode": "managed_identity",
                "subscriptions": [
                    {"subscriptionId": "sub-1", "label": "Production"},
                ],
            }
        )
        start = date(2026, 7, 1)
        records = []
        for index in range(32):  # through 2026-08-01
            usage_date = start + timedelta(days=index)
            records.append(
                {
                    "usageDate": usage_date.isoformat(),
                    "costType": "AmortizedCost",
                    "subscriptionId": "sub-1",
                    "resourceId": "/subscriptions/sub-1/resources/vm-1",
                    "serviceName": "Virtual Machines",
                    "amount": 100 + index % 7,
                    "currency": "USD",
                    "source": "test",
                }
            )
        self.database.store_daily_cost_scope(
            "history-1",
            "sub-1",
            "AmortizedCost",
            records,
            start_date=start,
            end_date=date(2026, 8, 1),
        )

        # Default is as-of the finalized horizon, matching the rest of Flux
        # reporting: a period with no finalized days yet is honestly empty
        # with an as-of note, never inverted and never silently extended
        # into unfinalized days.
        report = self.database.cost_report(
            cost_type="AmortizedCost", start_date=date(2026, 8, 1)
        )
        self.assertEqual(report["period"]["start"], "2026-08-01")
        self.assertIsNone(report["period"]["end"])
        self.assertIn("as of 2026-07-30", report["period"]["note"])
        self.assertIn("explicit endDate", report["period"]["note"])
        self.assertEqual(report["summary"]["totalCost"], 0)
        self.assertEqual(report["summary"]["previousCost"], 0)
        self.assertIsNone(report["period"]["previousStart"])

        # An explicit endDate is the deliberate opt-in to unfinalized rows.
        explicit = self.database.cost_report(
            cost_type="AmortizedCost",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
        )
        self.assertEqual(explicit["period"]["end"], "2026-08-01")
        self.assertEqual(explicit["summary"]["totalCost"], 103)
        self.assertEqual(explicit["summary"]["previousCost"], 102)

        empty = self.database.cost_report(
            cost_type="AmortizedCost", start_date=date(2026, 8, 5)
        )
        self.assertIsNone(empty["period"]["end"])
        self.assertTrue(empty["period"]["note"])
        self.assertNotIn("explicit endDate", empty["period"]["note"])
        self.assertEqual(empty["summary"]["totalCost"], 0)
        self.assertEqual(empty["summary"]["previousCost"], 0)
        self.assertIsNone(empty["period"]["previousStart"])

    def test_succeeded_empty_scope_counts_as_covered(self):
        """An empty subscription is covered, not unavailable.

        An empty subscription has zero resources and zero billed cost from
        both APIs; its collections succeed with zero rows. It must not
        read as "1 configured scope unavailable" forever.
        """
        self.database.save_integration(
            {
                "name": "Azure",
                "tenantId": "tenant",
                "enabled": True,
                "authMode": "managed_identity",
                "subscriptions": [
                    {"subscriptionId": "sub-empty", "label": "Empty"},
                ],
            }
        )
        self.database.start_cost_history_run("run-1", 1)
        self.database.begin_cost_history_scope(
            "run-1", "sub-empty", "AmortizedCost",
            date(2026, 7, 1), date(2026, 7, 30),
        )
        self.database.finish_cost_history_scope(
            "run-1", "sub-empty", "AmortizedCost",
            status="succeeded", row_count=0,
        )
        report = self.database.cost_report(cost_type="AmortizedCost")
        scope = report["dataCoverage"]["scopes"][0]
        self.assertTrue(scope["available"])
        self.assertTrue(scope["complete"])
        self.assertEqual(report["dataCoverage"]["availableScopes"], 1)

    def test_service_names_normalize_and_source_shift_is_disclosed(self):
        """Pure renames unify at ingestion; grouping shifts are disclosed.

        The mid-July FOCUS cutover made service movers look like Storage
        dropped to zero while Virtual Machines doubled -- labeling, not real
        movement.
        """
        self.database.save_integration(
            {
                "name": "Azure",
                "tenantId": "tenant",
                "enabled": True,
                "authMode": "managed_identity",
                "subscriptions": [
                    {"subscriptionId": "sub-1", "label": "Production"},
                ],
            }
        )

        def records(start, days, service, source):
            return [
                {
                    "usageDate": (start + timedelta(days=i)).isoformat(),
                    "costType": "AmortizedCost",
                    "subscriptionId": "sub-1",
                    "resourceId": "/subscriptions/sub-1/resources/vm-1",
                    "serviceName": service,
                    "amount": 100,
                    "currency": "USD",
                    "source": source,
                }
                for i in range(days)
            ]

        self.database.store_daily_cost_scope(
            "june", "sub-1", "AmortizedCost",
            records(date(2026, 6, 1), 30, "Storage Accounts",
                    "azure_cost_management_query"),
            start_date=date(2026, 6, 1), end_date=date(2026, 6, 30),
        )
        self.database.store_daily_cost_scope(
            "july", "sub-1", "AmortizedCost",
            records(date(2026, 7, 1), 30, "Virtual Machines",
                    "azure_focus_export"),
            start_date=date(2026, 7, 1), end_date=date(2026, 7, 30),
        )

        june = self.database.cost_report(
            cost_type="AmortizedCost",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 30),
        )
        self.assertEqual(june["byService"][0]["name"], "Storage")

        july = self.database.cost_report(
            cost_type="AmortizedCost",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 30),
        )
        shift_notes = [
            item for item in july["lineage"]["limitations"]
            if "different cost sources" in item
        ]
        self.assertTrue(
            shift_notes,
            "an all-FOCUS period compared against an all-Query period "
            "must disclose the source shift",
        )
        self.assertIn("Resource-level movers are unaffected", shift_notes[0])
        self.assertFalse(
            [
                item for item in june["lineage"]["limitations"]
                if "different cost sources" in item
            ],
            "same-source periods must not carry the disclosure",
        )

    def test_service_name_backfill_migration_relabels_legacy_rows(self):
        with self.database.connect() as db:
            db.execute(
                """
                INSERT INTO daily_cost_history VALUES (
                    'legacy', ?, DATE '2026-05-01', 'AmortizedCost', 'sub-1',
                    '/subscriptions/sub-1/resources/db-1',
                    'Azure SQL Database', 42, 'USD', 'azure_focus_export'
                )
                """,
                [datetime(2026, 5, 1, tzinfo=timezone.utc)],
            )
            db.execute(
                "DELETE FROM schema_migrations "
                "WHERE version = '20260801_canonical_service_names'"
            )
        self.database.init()
        with self.database.connect(read_only=True) as db:
            name = db.execute(
                "SELECT service_name FROM daily_cost_history "
                "WHERE snapshot_id = 'legacy'"
            ).fetchone()[0]
        self.assertEqual(name, "SQL Database")

    def test_policy_posture_publishes_latest_complete_snapshot(self):
        self.database.store_policy_posture(
            "policy-1",
            [
                {
                    "subscriptionId": "sub-1",
                    "subscriptionName": "Production",
                    "assignmentId": "/assignments/secure",
                    "assignmentName": "Secure baseline",
                    "evaluatedCount": 10,
                    "compliantCount": 7,
                    "nonCompliantCount": 2,
                    "exemptCount": 1,
                    "unknownCount": 0,
                    "resourceCount": 8,
                    "definitionCount": 3,
                }
            ],
            [
                {
                    "subscriptionId": "sub-1",
                    "subscriptionName": "Production",
                    "assignmentId": "/assignments/secure",
                    "assignmentName": "Secure baseline",
                    "definitionId": "/definitions/encryption",
                    "definitionName": "Require encryption",
                    "complianceState": "NonCompliant",
                    "resourceId": "/subscriptions/sub-1/resources/vm-1",
                    "resourceName": "vm-1",
                    "resourceType": "microsoft.compute/virtualmachines",
                    "region": "westus3",
                    "exemptionId": "",
                    "evaluatedAt": "2026-07-25T00:00:00Z",
                }
            ],
        )
        report = self.database.policy_report(
            subscription_id="sub-1",
            assignment_id="/assignments/secure",
            compliance_state="NonCompliant",
        )
        self.assertEqual(report["summary"]["assignmentCount"], 1)
        self.assertEqual(report["summary"]["compliancePercent"], 70.0)
        self.assertEqual(report["assignments"][0]["nonCompliant"], 2)
        self.assertEqual(report["resources"][0]["resourceName"], "vm-1")

    def test_anomaly_review_is_joined_to_immutable_signal(self):
        with self.database.connect() as db:
            db.execute(
                """
                INSERT INTO cost_anomaly_runs VALUES (
                    'run-1', current_timestamp, 'succeeded', DATE '2026-07-20',
                    1, 1, 0, 'complete', 'test-v1'
                )
                """
            )
            db.execute(
                """
                INSERT INTO cost_anomaly_snapshots (
                    run_id, evaluated_at, evaluation_date, cost_type,
                    scope_type, scope_id, subscription_id, resource_id,
                    resource_name, resource_type, resource_group, service_name,
                    current_amount, baseline_points, baseline_median, mad,
                    k_score, previous_week_amount, absolute_change,
                    percent_change, status, severity, currency, reason,
                    method_version
                ) VALUES (
                    'run-1', current_timestamp, DATE '2026-07-20',
                    'AmortizedCost', 'resource', '/resource/one', 'sub-1',
                    '/resource/one', 'one', 'test/type', 'rg', 'Compute',
                    200, 6, 100, 10, 6.7, 105, 100, 100,
                    'anomalous', 'high', 'USD', 'Spike', 'test-v1'
                )
                """
            )
        self.database.store_daily_cost_scope(
            "history-anomaly",
            "sub-1",
            "AmortizedCost",
            [
                {
                    "usageDate": "2026-07-13",
                    "costType": "AmortizedCost",
                    "subscriptionId": "sub-1",
                    "resourceId": "/resource/one",
                    "serviceName": "Compute",
                    "amount": 105,
                    "currency": "USD",
                    "source": "test",
                },
                {
                    "usageDate": "2026-07-20",
                    "costType": "AmortizedCost",
                    "subscriptionId": "sub-1",
                    "resourceId": "/resource/one",
                    "serviceName": "Compute",
                    "amount": 200,
                    "currency": "USD",
                    "source": "test",
                },
            ],
        )
        self.database.review_cost_anomaly(
            run_id="run-1",
            cost_type="AmortizedCost",
            scope_type="resource",
            scope_id="/resource/one",
            review_status="investigating",
            note="Owner validation requested.",
            updated_by="admin@example.com",
        )
        result = self.database.cost_anomalies(
            cost_type="AmortizedCost",
            status="anomalous",
        )
        self.assertEqual(result["items"][0]["reviewStatus"], "investigating")
        self.assertEqual(
            result["items"][0]["reviewNote"],
            "Owner validation requested.",
        )
        contributors = self.database.cost_anomaly_contributors(
            run_id="run-1",
            cost_type="AmortizedCost",
            scope_type="resource",
            scope_id="/resource/one",
        )
        self.assertEqual(contributors[0]["change"], 95)


if __name__ == "__main__":
    unittest.main()


class TagHygieneReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "tags.duckdb")
        self.database.init()
        self.database.seed_demo()

    def tearDown(self):
        self.temp.cleanup()

    def test_report_shape_and_percentages(self):
        report = self.database.tag_hygiene_report(required_tags=("env",))
        summary = report["summary"]
        self.assertGreater(summary["resourceCount"], 0)
        self.assertIn("taggedPercent", summary)
        self.assertEqual(summary["requiredTags"], ["env"])
        self.assertEqual(
            [row["tag"] for row in report["missingByRequiredTag"]], ["env"]
        )
        for bucket in report["bySubscription"]:
            self.assertLessEqual(bucket["tagged"], bucket["resources"])
        for item in report["topUntagged"]:
            self.assertGreater(item["monthlyCost"], 0)

    def test_excluded_types_reduce_assessed_count(self):
        full = self.database.tag_hygiene_report()
        first_type = None
        with self.database.connect(read_only=True) as db:
            row = db.execute(
                "SELECT resource_type FROM resources_current LIMIT 1"
            ).fetchone()
            first_type = row[0] if row else None
        if not first_type:
            self.skipTest("demo seed produced no resources")
        reduced = self.database.tag_hygiene_report(
            excluded_types=(first_type,)
        )
        self.assertLess(
            reduced["summary"]["resourceCount"],
            full["summary"]["resourceCount"],
        )
        self.assertGreater(reduced["summary"]["excludedCount"], 0)


class AllocationReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "alloc.duckdb")
        self.database.init()

    def tearDown(self):
        self.temp.cleanup()

    def _seed(self, resources):
        snapshot_id = "alloc-snap"
        self.database.store_snapshot(
            snapshot_id,
            [
                {
                    "resourceId": item["id"],
                    "name": item["id"].split("/")[-1],
                    "resourceType": "microsoft.compute/virtualmachines",
                    "subscriptionId": "sub",
                    "subscriptionName": "Production",
                    "resourceGroup": "rg",
                    "region": "westus3",
                    "tags": item.get("tags", {}),
                }
                for item in resources
            ],
            costs=[
                {
                    "resourceId": item["id"],
                    "subscriptionId": "sub",
                    "amount": item["cost"],
                    "costType": "ActualCost",
                    "currency": "USD",
                    "periodStart": date.today().replace(day=1).isoformat(),
                    "periodEnd": date.today().isoformat(),
                }
                for item in resources
            ],
            cost_scopes=[("sub", "ActualCost")],
        )

    def test_unconfigured_report_is_explicit(self):
        report = self.database.allocation_report()
        self.assertFalse(report["configured"])

    def test_allocation_with_shared_proration(self):
        self._seed([
            {"id": "/r/a", "cost": 60.0, "tags": {"cost-center": "Brands"}},
            {"id": "/r/b", "cost": 30.0, "tags": {"Cost-Center": "Supply"}},
            {"id": "/r/c", "cost": 9.0, "tags": {"cost-center": "Shared"}},
            {"id": "/r/d", "cost": 11.0},
        ])
        self.database.save_allocation_config(["cost-center"], ["shared"])
        report = self.database.allocation_report()
        self.assertTrue(report["configured"])
        summary = report["summary"]
        self.assertEqual(summary["totalMonthlyCost"], 110.0)
        self.assertEqual(summary["sharedPool"], 9.0)
        self.assertEqual(summary["unallocatedCost"], 11.0)
        centers = {item["name"]: item for item in report["centers"]}
        # Shared 9.0 splits 2:1 with direct spend 60/30.
        self.assertEqual(centers["Brands"]["sharedAllocation"], 6.0)
        self.assertEqual(centers["Supply"]["sharedAllocation"], 3.0)
        self.assertEqual(centers["Brands"]["totalCost"], 66.0)
        self.assertEqual(centers["Supply"]["totalCost"], 33.0)

    def test_config_round_trip(self):
        saved = self.database.save_allocation_config(
            [" cost-center ", ""], ["Shared "]
        )
        self.assertEqual(saved["costCenterTags"], ["cost-center"])
        self.assertEqual(saved["sharedValues"], ["Shared"])
        self.assertIsNotNone(saved["updatedAt"])


class FocusAnalyticsReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "focus.duckdb")
        self.database.init()

    def tearDown(self):
        self.temp.cleanup()

    def _insert_charge(self, charge_id, effective, listed, commitment="", status=""):
        import json as json_module
        raw = {"CommitmentDiscountStatus": status} if status else {}
        with self.database.connect() as db:
            db.execute(
                """
                INSERT INTO focus_cost_charges (
                    charge_id, manifest_id, charge_period_start,
                    billed_cost, effective_cost, list_cost,
                    billing_currency, charge_category, charge_class,
                    charge_frequency, charge_description, pricing_category,
                    consumed_unit, pricing_unit,
                    commitment_discount_id, commitment_discount_name,
                    commitment_discount_category, commitment_discount_type,
                    service_category, service_name, resource_id,
                    resource_name, resource_type, resource_group,
                    subscription_id, subscription_name, provider_name,
                    publisher_name, region_name, sku_id, sku_price_id,
                    meter_id, meter_name, meter_category, meter_subcategory,
                    tags_json, raw_json
                ) VALUES (
                    ?, 'm1', now(), ?, ?, ?, 'USD', 'Usage', 'Standard',
                    'Usage-Based', 'test', 'Standard', 'Hours', 'Hours',
                    ?, ?, 'Reservation', 'Reserved Instance',
                    'Compute', 'Virtual Machines', '/r/x', 'x', 'vm', 'rg',
                    'sub', 'Production', 'Microsoft', 'Microsoft', 'westus3',
                    'sku', 'skuP', 'meter', 'meter', 'Compute', 'VM',
                    '{}', ?
                )
                """,
                [
                    charge_id, effective, effective, listed,
                    commitment, commitment or "",
                    json_module.dumps(raw),
                ],
            )

    def test_unavailable_without_focus_data(self):
        report = self.database.focus_analytics_report()
        self.assertFalse(report["available"])

    def test_commitment_utilization_and_discounts(self):
        self._insert_charge("c1", 70.0, 100.0, commitment="ri-1")
        self._insert_charge("c2", 10.0, 10.0, commitment="ri-1", status="Unused")
        self._insert_charge("c3", 20.0, 25.0)
        report = self.database.focus_analytics_report()
        self.assertTrue(report["available"])
        commitment = report["commitment"]
        self.assertEqual(commitment["committedEffectiveCost"], 80.0)
        self.assertEqual(commitment["onDemandEffectiveCost"], 20.0)
        self.assertEqual(commitment["coveragePercent"], 80.0)
        ri = commitment["commitments"][0]
        self.assertEqual(ri["usedCost"], 70.0)
        self.assertEqual(ri["unusedCost"], 10.0)
        self.assertEqual(ri["utilizationPercent"], 87.5)
        pricing = report["pricing"]
        self.assertEqual(pricing["listCost"], 135.0)
        self.assertEqual(pricing["effectiveCost"], 100.0)
        self.assertEqual(pricing["discountRealized"], 35.0)


class SavingsLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "savings.duckdb")
        self.database.init()

    def tearDown(self):
        self.temp.cleanup()

    def _seed_cost(self, resource_id, amount):
        self.database.store_snapshot(
            f"snap-{resource_id.split('/')[-1]}-{amount}",
            [
                {
                    "resourceId": resource_id,
                    "name": resource_id.split("/")[-1],
                    "resourceType": "microsoft.compute/virtualmachines",
                    "subscriptionId": "sub",
                    "subscriptionName": "Production",
                    "resourceGroup": "rg",
                    "region": "westus3",
                    "tags": {},
                }
            ],
            costs=[
                {
                    "resourceId": resource_id,
                    "subscriptionId": "sub",
                    "amount": amount,
                    "costType": "ActualCost",
                    "currency": "USD",
                    "periodStart": date.today().replace(day=1).isoformat(),
                    "periodEnd": date.today().isoformat(),
                }
            ],
            cost_scopes=[("sub", "ActualCost")],
        )

    def test_implementation_captures_baseline_and_measures_realization(self):
        self._seed_cost("/r/vm-1", 100.0)
        result = self.database.set_opportunity_lifecycle(
            "opp-1", "implemented",
            resource_id="/r/vm-1", estimated_monthly_savings=40.0,
        )
        self.assertEqual(result["baselineMonthlyCost"], 100.0)
        # Cost drops after right-sizing: realized = baseline - current.
        self._seed_cost("/r/vm-1", 65.0)
        report = self.database.savings_report()
        self.assertEqual(report["summary"]["implementedCount"], 1)
        self.assertEqual(report["summary"]["realizedMonthly"], 35.0)
        item = report["items"][0]
        self.assertEqual(item["baselineMonthlyCost"], 100.0)
        self.assertEqual(item["currentMonthlyCost"], 65.0)
        self.assertEqual(item["realizedMonthlySavings"], 35.0)

    def test_realized_savings_never_negative(self):
        self._seed_cost("/r/vm-2", 50.0)
        self.database.set_opportunity_lifecycle(
            "opp-2", "implemented", resource_id="/r/vm-2",
        )
        self._seed_cost("/r/vm-2", 80.0)
        report = self.database.savings_report()
        self.assertEqual(report["items"][0]["realizedMonthlySavings"], 0.0)

    def test_lifecycle_annotates_opportunity_listing(self):
        self.database.set_opportunity_lifecycle("opp-3", "accepted")
        statuses = self.database.opportunity_lifecycles()
        self.assertEqual(statuses["opp-3"], "accepted")


class BudgetAndUnitEconomicsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "budget.duckdb")
        self.database.init()

    def tearDown(self):
        self.temp.cleanup()

    def _seed_history(self, daily_amount, as_of, days=10):
        """Seed `days` days of actuals ending at as_of's finalized horizon."""
        end = as_of - timedelta(days=2)
        start = end - timedelta(days=days - 1)
        records = [
            {
                "usageDate": (start + timedelta(days=index)).isoformat(),
                "costType": "ActualCost",
                "subscriptionId": "sub-1",
                "resourceId": "/r/vm",
                "serviceName": "Virtual Machines",
                "amount": daily_amount,
                "currency": "USD",
                "source": "test",
            }
            for index in range(days)
        ]
        self.database.store_daily_cost_scope(
            "budget-history", "sub-1", "ActualCost", records,
            start_date=start, end_date=end,
        )

    def test_unconfigured_budget_report(self):
        self.assertFalse(self.database.budget_report()["configured"])

    def test_budget_projection_and_status(self):
        # Fixed as_of (mid-month) so elapsed-day math is independent of
        # whatever day the suite actually runs on.
        as_of = date(2026, 6, 12)
        self._seed_history(100.0, as_of=as_of, days=10)
        self.database.save_budget_targets(
            [
                {"scopeType": "estate", "scopeId": "", "monthlyAmount": 10000.0},
                {"scopeType": "subscription", "scopeId": "SUB-1", "monthlyAmount": 1000.0},
            ],
            updated_by="tester",
        )
        report = self.database.budget_report(as_of=as_of)
        self.assertTrue(report["configured"])
        estate = next(t for t in report["targets"] if t["scopeType"] == "estate")
        subscription = next(
            t for t in report["targets"] if t["scopeType"] == "subscription"
        )
        self.assertGreater(estate["mtdActual"], 0)
        self.assertEqual(estate["mtdActual"], subscription["mtdActual"])
        # 10 elapsed MTD days at 100/day projects near 3000/month over June's
        # 30 days: well under the 10k estate budget, far over the 1k target.
        self.assertEqual(estate["status"], "on_track")
        self.assertEqual(subscription["status"], "over")

    def test_budget_report_first_of_month_does_not_roll_back(self):
        # Regression: the finalized-data lag (2 days) must not roll
        # month_start/elapsed_days back into the prior month on the 1st/2nd
        # of a month. Seed all of July with heavy spend so a buggy rollback
        # (reporting July's near-complete totals as August's MTD) would show
        # up immediately.
        self._seed_history(500.0, as_of=date(2026, 8, 2), days=31)
        self.database.save_budget_targets(
            [{"scopeType": "estate", "scopeId": "", "monthlyAmount": 10000.0}],
            updated_by="tester",
        )
        report = self.database.budget_report(as_of=date(2026, 8, 2))
        self.assertEqual(report["period"]["monthStart"], "2026-08-01")
        self.assertEqual(report["period"]["elapsedFinalizedDays"], 0)
        estate = report["targets"][0]
        self.assertEqual(estate["mtdActual"], 0)
        self.assertEqual(estate["status"], "on_track")

    def test_unit_economics_by_configured_tag(self):
        self.database.save_allocation_config(
            [], [], unit_tag="brand", unit_label="Brand"
        )
        self.database.store_snapshot(
            "unit-snap",
            [
                {
                    "resourceId": "/r/a", "name": "a",
                    "resourceType": "microsoft.compute/virtualmachines",
                    "subscriptionId": "sub", "subscriptionName": "Prod",
                    "resourceGroup": "rg", "region": "westus3",
                    "tags": {"Brand": "Terra"},
                },
                {
                    "resourceId": "/r/b", "name": "b",
                    "resourceType": "microsoft.compute/virtualmachines",
                    "subscriptionId": "sub", "subscriptionName": "Prod",
                    "resourceGroup": "rg", "region": "westus3",
                    "tags": {},
                },
            ],
            costs=[
                {
                    "resourceId": "/r/a", "subscriptionId": "sub",
                    "amount": 75.0, "costType": "ActualCost",
                    "currency": "USD",
                    "periodStart": date.today().replace(day=1).isoformat(),
                    "periodEnd": date.today().isoformat(),
                },
                {
                    "resourceId": "/r/b", "subscriptionId": "sub",
                    "amount": 25.0, "costType": "ActualCost",
                    "currency": "USD",
                    "periodStart": date.today().replace(day=1).isoformat(),
                    "periodEnd": date.today().isoformat(),
                },
            ],
            cost_scopes=[("sub", "ActualCost")],
        )
        report = self.database.unit_economics_report()
        self.assertTrue(report["configured"])
        self.assertEqual(report["summary"]["dimensionLabel"], "Brand")
        self.assertEqual(report["units"][0]["name"], "Terra")
        self.assertEqual(report["units"][0]["monthlyCost"], 75.0)
        self.assertEqual(report["summary"]["unattributedCost"], 25.0)

    def test_executive_summary_composes(self):
        summary = self.database.executive_summary()
        self.assertIn("generatedAt", summary)
        self.assertIn("savings", summary)
        self.assertIn("serviceComposition", summary)
        self.assertIn("billingServices", summary["serviceComposition"])
        self.assertIn("economicCategories", summary["serviceComposition"])
        self.assertIsNone(summary["budgets"])


class FleetTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "fleet.duckdb")
        self.database.init()
        self.database.seed_demo()

    def tearDown(self):
        self.temp.cleanup()

    def test_returns_many_resources_in_one_call(self):
        report = self.database.fleet_telemetry(resource_type="")
        self.assertGreater(report["returned"], 1)
        self.assertEqual(report["returned"], len(report["items"]))
        self.assertGreaterEqual(report["matching"], report["returned"])
        item = report["items"][0]
        for key in (
            "resourceId", "sku", "region", "cpuP95", "coveragePercent",
            "actualMonthlyCost", "telemetryStatus",
        ):
            self.assertIn(key, item)

    def test_truncation_is_disclosed_not_silent(self):
        report = self.database.fleet_telemetry(resource_type="", limit=1)
        self.assertEqual(report["returned"], 1)
        if report["matching"] > 1:
            self.assertTrue(report["truncated"])
            self.assertTrue(report["limitations"])
            self.assertIn("match", report["limitations"][0])

    def test_filters_narrow_the_fleet(self):
        everything = self.database.fleet_telemetry(resource_type="")
        subscription = everything["items"][0]["subscriptionId"]
        filtered = self.database.fleet_telemetry(
            resource_type="", subscription_id=subscription
        )
        self.assertLessEqual(filtered["matching"], everything["matching"])
        for item in filtered["items"]:
            self.assertEqual(item["subscriptionId"], subscription)

    def test_resources_without_telemetry_are_labeled(self):
        report = self.database.fleet_telemetry(resource_type="")
        for item in report["items"]:
            if item["cpuP95"] is None:
                self.assertEqual(item["telemetryStatus"], "no_cpu_evidence")
            else:
                self.assertEqual(item["telemetryStatus"], "covered")
