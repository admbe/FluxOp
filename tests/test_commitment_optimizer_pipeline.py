"""End-to-end pipeline tests over a seeded governed DuckDB.

The fixtures are synthetic but schema-faithful: FOCUS hourly charges, an
MCA-style negotiated price sheet, retail price snapshots, reservation and
Savings Plan inventory. They verify grain detection, data-quality gates,
portfolio persistence, manifests, and purchase safety refusals.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from api.commitment_optimizer_pipeline import (
    apportion_expected_savings,
    compare_optimizer_runs,
    generate_purchase_manifest,
    list_optimizer_runs,
    optimizer_run_detail,
    optimizer_status,
    record_recommendation_decision,
    run_commitment_optimization,
    save_optimizer_override,
)
from api.database import FluxDatabase, utc_now

PRICE_SHEET_CSV = """meterId,meterName,serviceFamily,product,skuId,unitOfMeasure,priceType,unitPrice,basePrice,marketPrice,currency,term
m-d4,D4s v5,Compute,Virtual Machines Dsv5,SKU-D4,1 Hour,Consumption,0.10,0.12,0.12,USD,
m-d4,D4s v5,Compute,Virtual Machines Dsv5,SKU-D4,1 Hour,SavingsPlan,0.065,0.12,0.12,USD,1 Year
m-d4,D4s v5,Compute,Virtual Machines Dsv5,SKU-D4,1 Year,Reservation,438.0,525.6,525.6,USD,1 Year
"""

SUB = "11111111-1111-1111-1111-111111111111"


class CommitmentOptimizerPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "c.duckdb")
        self.database.init()

    def tearDown(self):
        self.temp.cleanup()

    def _seed_focus(
        self,
        *,
        days=10,
        grain_hours=1,
        vms_per_hour=2,
        duplicate=False,
        end=None,
        pricing_category="Standard",
    ) -> str:
        manifest_id = f"manifest-{uuid4()}"
        end = end or (utc_now() - timedelta(days=1)).replace(
            minute=0, second=0, microsecond=0
        )
        start = end - timedelta(days=days)
        with self.database.connect() as db:
            db.execute(
                """
                INSERT INTO focus_export_manifests VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'imported',
                    ?, ?, ?, ?, ?, ?, ''
                )
                """,
                [
                    manifest_id,
                    "run-1",
                    f"/{manifest_id}/manifest.json",
                    "focus-export",
                    "export-run-1",
                    SUB,
                    "Test subscription",
                    start.date(),
                    end.date(),
                    end,
                    end,
                    "1",
                    days * 24,
                    1024,
                    "USD",
                    0.2 * days * 24,
                    0.2 * days * 24,
                ],
            )
            db.execute(
                f"""
                INSERT INTO focus_cost_charges
                SELECT
                    'charge-' || row_number() OVER (ORDER BY hour, vm),
                    ?,
                    hour,
                    hour + INTERVAL '{int(grain_hours)}' HOUR,
                    date_trunc('month', hour),
                    date_trunc('month', hour) + INTERVAL 1 MONTH,
                    0.2, 0.2, 0.2, 0.24,
                    'USD', 'Usage', 'Usage', 'Usage-based', 'D4s v5',
                    '{pricing_category}', 1.0, '1 Hour', 1.0, '1 Hour', 0.10, 0.12,
                    '', '', '', '', 'Compute', 'Virtual Machines',
                    '/subscriptions/' || ? ||
                        '/resourceGroups/rg/providers/Microsoft.Compute'
                        '/virtualMachines/vm-' || vm,
                    'vm-' || vm,
                    'Microsoft.Compute/virtualMachines', 'rg-test', ?,
                    'Test subscription', 'Microsoft.Compute', 'Microsoft',
                    'eastus', 'SKU-D4', 'PRICE-D4', 'm-d4', 'D4s v5',
                    'Virtual Machines', 'Dsv5', '{{}}', '{{}}'
                FROM range(?::TIMESTAMPTZ, ?::TIMESTAMPTZ,
                         INTERVAL '{int(grain_hours)}' HOUR)
                    AS hours(hour),
                     range(?) AS vms(vm)
                """,
                [
                    manifest_id,
                    SUB,
                    SUB,
                    start,
                    end,
                    vms_per_hour,
                ],
            )
            if duplicate:
                db.execute(
                    """
                    INSERT INTO focus_cost_charges
                    SELECT
                        charge_id || '-dup', manifest_id, charge_period_start,
                        charge_period_end, billing_period_start,
                        billing_period_end, billed_cost, effective_cost,
                        contracted_cost, list_cost, billing_currency,
                        charge_category, charge_class, charge_frequency,
                        charge_description, pricing_category,
                        consumed_quantity, consumed_unit, pricing_quantity,
                        pricing_unit, contracted_unit_price, list_unit_price,
                        commitment_discount_id, commitment_discount_name,
                        commitment_discount_category,
                        commitment_discount_type, service_category,
                        service_name, resource_id, resource_name,
                        resource_type, resource_group, subscription_id,
                        subscription_name, provider_name, publisher_name,
                        region_name, sku_id, sku_price_id, meter_id,
                        meter_name, meter_category, meter_subcategory,
                        tags_json, raw_json
                    FROM focus_cost_charges
                    ORDER BY charge_id DESC
                    LIMIT 1
                    """
                )
        return manifest_id

    def _seed_price_sheet(self) -> None:
        path = Path(self.temp.name) / "pricesheet.csv"
        path.write_text(PRICE_SHEET_CSV, encoding="utf-8")
        self.database.store_price_sheet([path])

    def _seed_retail(self) -> None:
        with self.database.connect() as db:
            db.execute(
                """
                INSERT INTO retail_price_snapshots VALUES (
                    ?, ?, 'eastus', 'Standard_D4s_v5', 'linux', '', 'linux',
                    'USD', 'matched', 0.12, 87.6, 87.6, 0.0, 43.8, 525.6,
                    47.45, 730.0, 'm-d4', 'D4s v5', 'Virtual Machines Dsv5',
                    'D4s v5', '1 Hour', ?, 1, 'test', '', '', '{}'
                )
                """,
                [
                    f"retail-{uuid4()}",
                    datetime(2026, 7, 10, tzinfo=timezone.utc),
                    datetime(2026, 6, 1, tzinfo=timezone.utc),
                ],
            )

    def _seed_commitment_feeds(
        self,
        *,
        sp_hourly=None,
        sp_expired=False,
        sp_expiry="2027-01-01",
        sp_utilization=None,
        reservation=None,
    ) -> None:
        reservations = []
        if reservation:
            reservations = [
                {
                    "reservationId": "/providers/reservation/1",
                    "orderId": "order-ri-1",
                    "displayName": "Existing RI",
                    "sku": reservation.get("sku", "Standard_D4s_v5"),
                    "resourceType": "VirtualMachines",
                    "region": reservation.get("region", "eastus"),
                    "quantity": reservation.get("quantity", 1),
                    "term": "P1Y",
                    "scopeType": "Shared",
                    "state": "Succeeded",
                    "expiryDate": reservation.get("expiryDate"),
                    "utilization1d": reservation.get("utilization"),
                    "utilization7d": reservation.get("utilization"),
                    "utilization30d": reservation.get("utilization"),
                }
            ]
        self.database.store_commitments(
            f"commitments-{uuid4()}", reservations, []
        )
        plans = []
        if sp_hourly:
            plans = [
                {
                    "savingsPlanId": "/providers/sp/1",
                    "orderId": "order-1",
                    "displayName": "Existing SP",
                    "hourlyCommitment": sp_hourly,
                    "currency": "USD",
                    "term": "P1Y",
                    "scopeType": "Shared",
                    "appliedScopes": [],
                    "purchaseDate": "2026-01-01",
                    "expiryDate": "2025-01-01" if sp_expired else sp_expiry,
                    "billingPlan": "Monthly",
                    "state": "Succeeded",
                    "utilization1d": sp_utilization,
                    "utilization7d": sp_utilization,
                    "utilization30d": sp_utilization,
                }
            ]
        self.database.store_savings_plans(f"sp-{uuid4()}", plans, [])

    def _seed_rightsizing(self) -> None:
        with self.database.connect() as db:
            db.execute(
                """
                INSERT INTO rightsizing_recommendation_snapshots VALUES (
                    ?, ?, 'vm-0', 'vm-0', ?, 'Test subscription', 'rg-test',
                    'eastus', 'resize', 'candidate', 'Standard_D4s_v5',
                    'Standard_D2s_v5', 30, 'covered', 'azure_monitor',
                    3.0, 9.0, 40.0, 1000.0, 1000.0, 92.0, 20.0, 'USD',
                    'retail_price_difference', 'Low CPU utilization.',
                    '{}', 'test-method'
                )
                """,
                [
                    f"rr-{uuid4()}",
                    datetime(2026, 7, 10, tzinfo=timezone.utc),
                    SUB,
                ],
            )

    def test_full_run_purchase_ready(self):
        self._seed_focus(vms_per_hour=4)
        self._seed_price_sheet()
        self._seed_retail()
        self._seed_commitment_feeds()
        self._seed_rightsizing()

        result = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["readiness"], "PURCHASE_READY", result["gates"])
        self.assertEqual(result["currency"], "USD")
        summary = result["summary"]
        for name in ("payg", "existing_commitments", "ri_only", "sp_only", "blended"):
            self.assertIn(name, summary["portfolios"])
        self.assertTrue(summary["grain"]["hourly"])
        self.assertIn(result["recommendedPortfolio"], summary["portfolios"])
        self.assertTrue(result["recommendations"])

        runs = list_optimizer_runs(self.database)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["readiness"], "PURCHASE_READY")

        detail = optimizer_run_detail(self.database, result["runId"])
        self.assertTrue(detail["scenarios"])
        self.assertTrue(detail["recommendations"])
        hours = self.database.optimizer_hourly_evidence(result["runId"])
        self.assertTrue(hours)
        self.assertEqual(len(hours), 10 * 24)

        manifest = generate_purchase_manifest(
            self.database, result["runId"], actor="tester"
        )
        self.assertEqual(manifest["version"], 1)
        self.assertTrue(manifest["csv"].startswith("action,commitmentType"))
        self.assertTrue(manifest["hash"])
        again = generate_purchase_manifest(
            self.database, result["runId"], actor="tester"
        )
        self.assertEqual(again["version"], 2)

    def test_missing_price_sheet_blocks_purchase(self):
        self._seed_focus()
        self._seed_commitment_feeds()
        result = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        self.assertEqual(result["readiness"], "BLOCKED")
        with self.assertRaises(ValueError):
            generate_purchase_manifest(
                self.database, result["runId"], actor="tester"
            )

    def _eligible_rows(self, pricing_category: str) -> dict:
        self._seed_focus(vms_per_hour=4, pricing_category=pricing_category)
        self._seed_price_sheet()
        self._seed_retail()
        self._seed_commitment_feeds()
        result = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        return result["summary"]["usageStats"]

    def test_azure_standard_pricing_category_is_eligible(self):
        """Azure's FOCUS export says "Standard" where FOCUS says "OnDemand".

        Production had 17,360 usage rows and zero eligible ones because the
        pipeline matched only "ondemand". The fixtures said "OnDemand" too,
        so the suite stayed green while the optimizer could not price
        anything. Pin the value Azure actually emits.
        """
        stats = self._eligible_rows("Standard")
        self.assertGreater(stats["riEligibleRows"], 0)
        self.assertGreater(stats["spEligibleRows"], 0)
        self.assertEqual(stats["skippedNotOnDemandCompute"], 0)

    def test_focus_spec_ondemand_spelling_also_eligible(self):
        """Accepted as well, in case Azure aligns with the specification."""
        stats = self._eligible_rows("OnDemand")
        self.assertGreater(stats["riEligibleRows"], 0)

    def test_spot_usage_is_not_commitment_eligible(self):
        """No commitment covers spot, so those rows must stay out - and be
        counted on the way out rather than vanishing silently."""
        stats = self._eligible_rows("Dynamic")
        self.assertEqual(stats["riEligibleRows"], 0)
        self.assertEqual(stats["spEligibleRows"], 0)
        self.assertGreater(stats["skippedNotOnDemandCompute"], 0)

    def test_daily_grain_is_directional_only(self):
        self._seed_focus(grain_hours=24, vms_per_hour=1)
        self._seed_price_sheet()
        self._seed_retail()
        self._seed_commitment_feeds()
        result = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        self.assertEqual(result["readiness"], "DIRECTIONAL_ONLY")
        grain_gate = next(
            g for g in result["gates"] if g["name"] == "hourly_grain_explicit"
        )
        self.assertFalse(grain_gate["passed"])

    def test_duplicate_hours_are_flagged(self):
        self._seed_focus(duplicate=True)
        self._seed_price_sheet()
        self._seed_retail()
        self._seed_commitment_feeds()
        result = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        dup_gate = next(
            g for g in result["gates"] if g["name"] == "no_duplicate_hours"
        )
        self.assertFalse(dup_gate["passed"])
        self.assertIn(result["readiness"], ("DIRECTIONAL_ONLY", "BLOCKED"))

    def test_expired_savings_plan_excluded(self):
        self._seed_focus()
        self._seed_price_sheet()
        self._seed_retail()
        self._seed_commitment_feeds(sp_hourly=0.5, sp_expired=True)
        result = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        self.assertEqual(
            result["summary"]["existingCommitments"]["savingsPlans"], 0
        )

    def test_active_savings_plan_modeled_as_existing(self):
        self._seed_focus()
        self._seed_price_sheet()
        self._seed_retail()
        self._seed_commitment_feeds(sp_hourly=0.05)
        result = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        self.assertEqual(
            result["summary"]["existingCommitments"]["savingsPlans"], 1
        )
        existing = result["summary"]["portfolios"]["existing_commitments"]
        self.assertGreater(existing["spUtilization"], 0.0)

    def test_no_focus_data_blocks_with_message(self):
        self._seed_price_sheet()
        result = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        self.assertEqual(result["readiness"], "BLOCKED")
        self.assertIn("FOCUS", result["summary"]["error"])

    def test_recommendation_decision_workflow(self):
        self._seed_focus()
        self._seed_price_sheet()
        self._seed_retail()
        self._seed_commitment_feeds()
        result = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        rec = result["recommendations"][0]
        decision = record_recommendation_decision(
            self.database,
            rec["recommendationId"],
            decision="approved",
            actor="tester",
            note="looks right",
        )
        self.assertEqual(decision["decision"], "approved")
        detail = optimizer_run_detail(self.database, result["runId"])
        stored = next(
            r
            for r in detail["recommendations"]
            if r["recommendationId"] == rec["recommendationId"]
        )
        self.assertEqual(stored["decisionBy"], "tester")
        with self.assertRaises(KeyError):
            record_recommendation_decision(
                self.database, "missing", decision="approved", actor="t"
            )
        with self.assertRaises(ValueError):
            record_recommendation_decision(
                self.database,
                rec["recommendationId"],
                decision="maybe",
                actor="t",
            )

    def test_override_excludes_resource(self):
        self._seed_focus(vms_per_hour=2)
        self._seed_price_sheet()
        self._seed_retail()
        self._seed_commitment_feeds()
        override = save_optimizer_override(
            self.database,
            target_type="resource",
            target_id=(
                f"/subscriptions/{SUB}/resourceGroups/rg/providers"
                "/Microsoft.Compute/virtualMachines/vm-0"
            ),
            override_type="exclude",
            reason="decommission approved",
            actor="tester",
        )
        self.assertTrue(override["overrideId"])
        result = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        self.assertEqual(result["status"], "completed")

    def test_status_reports_configuration(self):
        status = optimizer_status(self.database)
        self.assertIn("algorithmVersion", status)
        self.assertEqual(status["riskProfiles"], ["aggressive", "balanced", "conservative"])

    def test_runs_are_versioned_and_immutable(self):
        self._seed_focus()
        self._seed_price_sheet()
        self._seed_retail()
        self._seed_commitment_feeds()
        first = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        second = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        self.assertNotEqual(first["runId"], second["runId"])
        runs = list_optimizer_runs(self.database)
        self.assertEqual(len(runs), 2)


class ContinuousReoptimizationTests(CommitmentOptimizerPipelineTests):
    """Backtests, review events, laddering, and run comparison (spec 14/17/20/21)."""

    def _seed_full(self, **focus_kwargs) -> None:
        self._seed_focus(**focus_kwargs)
        self._seed_price_sheet()
        self._seed_retail()
        self._seed_commitment_feeds()
        self._seed_rightsizing()

    def test_backtest_reported_for_long_lookback(self):
        self._seed_full(days=16)
        result = run_commitment_optimization(
            self.database, lookback_days=16, requested_by="tester"
        )
        backtest = result["summary"]["backtest"]
        if result["recommendedPortfolio"] != "payg":
            self.assertIsNotNone(backtest)
            self.assertEqual(backtest["holdoutHours"], 7 * 24)
            self.assertEqual(
                backtest["trainHours"] + backtest["holdoutHours"], 16 * 24
            )
            self.assertIn("stable", backtest)
            self.assertGreaterEqual(backtest["holdoutSavings"], 0.0)

    def test_backtest_omitted_for_short_lookback(self):
        self._seed_full(days=10)
        result = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        self.assertIsNone(result["summary"]["backtest"])

    def test_expiring_reservation_produces_renew_recommendation(self):
        self._seed_focus()
        self._seed_price_sheet()
        self._seed_retail()
        expiry = (utc_now() + timedelta(days=20)).date().isoformat()
        self._seed_commitment_feeds(
            reservation={"quantity": 1, "expiryDate": expiry, "utilization": 0.95}
        )
        self._seed_rightsizing()
        result = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        renewals = [
            rec for rec in result["recommendations"] if rec["action"] == "renew"
        ]
        self.assertEqual(len(renewals), 1)
        self.assertEqual(renewals[0]["evidence"]["trigger"], "commitment_expiry")
        self.assertEqual(renewals[0]["evidence"]["proposedDate"], expiry)
        events = result["summary"]["reviewEvents"]
        expiring = [e for e in events if e["type"] == "commitment_expiring"]
        self.assertEqual(len(expiring), 1)
        self.assertEqual(expiring[0]["commitmentType"], "reservation")

    def test_underutilized_expiring_reservation_produces_allow_expiry(self):
        self._seed_focus()
        self._seed_price_sheet()
        self._seed_retail()
        expiry = (utc_now() + timedelta(days=15)).date().isoformat()
        self._seed_commitment_feeds(
            reservation={"quantity": 1, "expiryDate": expiry, "utilization": 0.1}
        )
        self._seed_rightsizing()
        result = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        actions = [rec["action"] for rec in result["recommendations"]]
        self.assertIn("allow_expiry", actions)
        self.assertNotIn("renew", actions)

    def test_stale_usage_raises_review_event(self):
        self._seed_focus(end=utc_now() - timedelta(days=10))
        self._seed_price_sheet()
        self._seed_retail()
        self._seed_commitment_feeds()
        self._seed_rightsizing()
        result = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        types = [e["type"] for e in result["summary"]["reviewEvents"]]
        self.assertIn("usage_data_stale", types)

    def test_blocked_run_reports_data_quality_event(self):
        self._seed_focus()
        self._seed_commitment_feeds()
        result = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        self.assertEqual(result["readiness"], "BLOCKED")
        types = [e["type"] for e in result["summary"]["reviewEvents"]]
        self.assertIn("data_quality_degraded", types)

    def test_identical_rerun_reports_no_material_change(self):
        self._seed_full()
        run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        second = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        types = [e["type"] for e in second["summary"]["reviewEvents"]]
        self.assertNotIn("recommendation_changed", types)

    def test_case14_exclusion_reduces_purchase_size(self):
        def purchase_volume(result) -> float:
            total = 0.0
            for rec in result["recommendations"]:
                if rec["action"] in ("buy_now", "review"):
                    if rec["commitmentType"] == "reservation":
                        total += float(rec["quantity"])
                    elif rec["commitmentType"] == "savings_plan":
                        total += float(rec["hourlyCommitment"])
            return total

        self._seed_full(vms_per_hour=4)
        baseline = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        base_volume = purchase_volume(baseline)
        self.assertGreater(base_volume, 0.0)
        for vm in ("vm-0", "vm-1"):
            save_optimizer_override(
                self.database,
                target_type="resource",
                target_id=(
                    f"/subscriptions/{SUB}/resourceGroups/rg/providers"
                    f"/Microsoft.Compute/virtualMachines/{vm}"
                ),
                override_type="exclude",
                reason="decommission approved",
                actor="tester",
            )
        reduced = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        self.assertLess(purchase_volume(reduced), base_volume)

    def test_case15_delayed_rightsizing_limits_purchase_readiness(self):
        self._seed_focus(vms_per_hour=4)
        self._seed_price_sheet()
        self._seed_retail()
        self._seed_commitment_feeds()
        result = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        self.assertEqual(result["readiness"], "REVIEW_REQUIRED")
        purchases = [
            rec
            for rec in result["recommendations"]
            if rec["commitmentType"] in ("reservation", "savings_plan")
            and rec["action"] in ("buy_now", "review")
        ]
        self.assertTrue(purchases)
        for rec in purchases:
            self.assertEqual(rec["action"], "review")
            self.assertEqual(rec["status"], "review_required")
        deferred = [
            rec for rec in result["recommendations"] if rec["action"] == "defer"
        ]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(
            deferred[0]["evidence"]["trigger"], "rightsizing_confident"
        )
        self.assertIn("proposedDate", deferred[0]["evidence"])
        self.assertIn("costOfWaiting", deferred[0]["evidence"])

    def test_compare_runs_reports_deltas(self):
        self._seed_full()
        first = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        second = run_commitment_optimization(
            self.database, lookback_days=10, requested_by="tester"
        )
        comparison = compare_optimizer_runs(
            self.database, first["runId"], second["runId"]
        )
        self.assertEqual(comparison["base"]["runId"], first["runId"])
        self.assertEqual(comparison["compare"]["runId"], second["runId"])
        self.assertFalse(comparison["recommendationChanged"])
        self.assertFalse(comparison["materialChange"])
        self.assertAlmostEqual(
            comparison["deltas"]["annualizedSavings"], 0.0, places=2
        )
        with self.assertRaises(KeyError):
            compare_optimizer_runs(
                self.database, first["runId"], "opt-missing"
            )

    def test_mixed_price_sheet_currencies_fail_closed(self):
        """#54: >1 distinct currency in price_sheet_current must raise ValueError.

        Mirrors the FOCUS manifest mixed-currency guard — the optimizer must
        not silently pick the dominant currency and mix FX-naive rates into
        the baseline.
        """
        self._seed_focus(vms_per_hour=2)
        # Seed a single-currency price sheet then inject an EUR row directly
        # to simulate a retail-fallback leak without depending on store_price_sheet
        # validation (which is the code under test).
        self._seed_price_sheet()
        with self.database.connect() as db:
            db.execute(
                """INSERT INTO price_sheet_current VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    "m-eur", "EurSku", "Compute", "VM", "SKU-EUR",
                    "1 Hour", "Consumption", 0.09, 0.11, 0.11, "EUR", "1 Year", "2026-01-01",
                ],
            )
        from api.commitment_optimizer_pipeline import build_price_book

        with self.assertRaisesRegex(ValueError, r"distinct.*currenc"):
            build_price_book(self.database)
        # Also verify via full optimizer run that it surfaces as failed/BLOCKED
        with self.assertRaisesRegex(ValueError, r"distinct.*currenc"):
            run_commitment_optimization(
                self.database, lookback_days=10, requested_by="tester"
            )
        from api.commitment_optimizer_pipeline import list_optimizer_runs

        runs = list_optimizer_runs(self.database)
        self.assertEqual(runs[0]["status"], "failed")
        self.assertEqual(runs[0]["readiness"], "BLOCKED")
        self.assertIn("EUR", runs[0]["error"])
        self.assertIn("USD", runs[0]["error"])

        # Single-currency and empty are unaffected — verified by the other tests
        # (test_full_run_purchase_ready, test_missing_price_sheet_blocks_purchase).


if __name__ == "__main__":
    unittest.main()


class ApportionedSavingsTests(unittest.TestCase):
    """Purchase lines must not each report the whole portfolio's savings.

    Regression: every RI and SP recommendation carried
    portfolios[recommended].savings_vs_payg, so a plan with N purchase lines
    showed N times the true figure. These rows render as a per-row column in
    the purchase-plan table and are written per-row into the approved
    purchase-manifest CSV, where a reviewer totals the column.
    """

    def _plan(self):
        return [
            {"commitmentType": "reservation", "status": "purchase_ready",
             "expectedCost": 43800.0, "expectedSavings": 0.0},
            {"commitmentType": "reservation", "status": "purchase_ready",
             "expectedCost": 10950.0, "expectedSavings": 0.0},
            {"commitmentType": "reservation", "status": "purchase_ready",
             "expectedCost": 21900.0, "expectedSavings": 0.0},
            {"commitmentType": "savings_plan", "status": "purchase_ready",
             "expectedCost": 109500.0, "expectedSavings": 0.0},
            {"commitmentType": "payg_remainder", "status": "informational",
             "expectedCost": 5000.0, "expectedSavings": 0.0},
            {"commitmentType": "reservation", "status": "deferred",
             "expectedCost": 0.0, "expectedSavings": 0.0},
        ]

    def test_the_savings_column_totals_the_portfolio_figure(self):
        plan = self._plan()
        apportion_expected_savings(plan, 40000.0)
        total = sum(row["expectedSavings"] for row in plan)
        # Old behaviour put 40000.0 on each of the 4 purchase lines: 160000.
        self.assertAlmostEqual(total, 40000.0, places=1)

    def test_attribution_is_proportional_to_committed_cost(self):
        plan = self._plan()
        apportion_expected_savings(plan, 40000.0)
        # The SP line is 109500 / 186150 of the committed cost.
        self.assertAlmostEqual(plan[3]["expectedSavings"], 23529.41, places=2)
        self.assertAlmostEqual(plan[0]["expectedSavings"], 9411.76, places=2)

    def test_informational_and_deferred_rows_stay_at_zero(self):
        plan = self._plan()
        apportion_expected_savings(plan, 40000.0)
        self.assertEqual(plan[4]["expectedSavings"], 0.0)
        self.assertEqual(plan[5]["expectedSavings"], 0.0)

    def test_non_positive_savings_leaves_every_row_at_zero(self):
        plan = self._plan()
        apportion_expected_savings(plan, 0.0)
        self.assertEqual([row["expectedSavings"] for row in plan], [0.0] * 6)

