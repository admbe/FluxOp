from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase
from api.rightsizing_proposal import (
    FLUX_ACTOR,
    FLUX_BOARD_NAME,
    build_proposal,
    proposal_status,
)


def vm(name: str, sku: str, **overrides):
    value = {
        "vmKey": f"/subscriptions/sub/resourcegroups/rg/providers/vm/{name}",
        "name": name,
        "subscriptionName": "Production",
        "region": "westus3",
        "sku": sku,
        "action": "none",
        "targetSku": "",
        "reason": "",
        "windowDays": 90,
        "coveragePercent": 95.0,
        "noData": False,
        "observedMonthlyListCost": 100.0,
        "priceProfile": "linux",
        "licenseModel": "linux",
        "telemetrySource": "azure_monitor",
    }
    value.update(overrides)
    return value


class ProposalMathTests(unittest.TestCase):
    def build(self, vms, **overrides):
        inputs = {
            "waste_kinds_by_vm": {},
            "active_reservations": [],
            "reservation_recommendations": [],
            "retail_prices": {},
            "as_of": date(2026, 8, 4),
        }
        inputs.update(overrides)
        return build_proposal(vms, **inputs)

    def test_resize_action_uses_target_and_carries_performance_warning(self):
        result = self.build([
            vm(
                "api-1",
                "Standard_D8as_v5",
                action="resize",
                targetSku="Standard_D4as_v5",
            )
        ])
        self.assertEqual(result["buckets"][0]["sku"], "Standard_D4as_v5")
        self.assertIn("storage IOPS", result["assignments"][0]["note"])

    def test_shutdown_is_removed_before_commitment_baseline(self):
        result = self.build([vm("idle", "Standard_D2as_v5", action="shutdown")])
        self.assertEqual(result["assignments"][0]["bucketKey"], "__excluded__")
        self.assertEqual(result["counts"]["waste"], 1)
        self.assertEqual(result["buckets"], [])

    def test_isf_nets_existing_units_without_underbuying(self):
        result = self.build(
            [vm("small", "Standard_D2as_v5"), vm("large", "Standard_D4as_v5")],
            active_reservations=[{
                "sku": "Standard_D1as_v5",
                "region": "westus3",
                "quantity": 1,
                "expiryDate": "2027-11-01",
                "name": "existing-d1",
            }],
        )
        bucket = result["buckets"][0]
        self.assertEqual(bucket["sku"], "Standard_D4as_v5")
        self.assertEqual(bucket["refQuantity"], 2)
        self.assertIn("normalized units", bucket["note"])

    def test_retail_reconciliation_models_savings_and_ignores_advisor_totals(self):
        result = self.build(
            [vm("one", "Standard_D4as_v5"), vm("two", "Standard_D4as_v5")],
            retail_prices={
                ("westus3", "standard_d4as_v5", "linux"): {
                    "monthly_price": 100.0,
                    "monthly_compute_price": 100.0,
                    "monthly_license_price": 0.0,
                    "monthly_ri_1y": 60.0,
                    "ri_1y_upfront": 720.0,
                    "monthly_sp_1y": 70.0,
                    "license_model": "linux",
                }
            },
            reservation_recommendations=[{
                "region": "westus3",
                "sku": "Standard_D4as_v5",
                "term": "P1Y",
                "lookBack": "Last60Days",
                "recommendedQuantity": 2,
                "costWithoutCommitment": 2400,
                "costWithCommitment": 1800,
                "netSavings": 600,
            }],
        )
        bucket = result["buckets"][0]
        self.assertEqual(bucket["refMonthlyPayg"], 200.0)
        self.assertEqual(bucket["refMonthlyRi1y"], 120.0)
        self.assertEqual(bucket["refMonthlySavings"], 80.0)
        self.assertEqual(bucket["refRi1yUpfront"], 1440.0)
        self.assertIn("excluded from the savings total", bucket["note"])

    def test_shorter_valid_window_is_disclosed(self):
        result = self.build([vm("cyclical", "Standard_E2as_v5", windowDays=45)])
        self.assertIn("90-day", result["assignments"][0]["note"])

    def test_short_monitored_window_is_placed_with_provisional_warning(self):
        result = self.build([
            vm(
                "recently-monitored",
                "Standard_D4as_v5",
                windowDays=14,
                coveragePercent=95.0,
            )
        ])
        self.assertEqual(result["counts"]["noData"], 0)
        self.assertEqual(result["counts"]["provisional"], 1)
        self.assertEqual(result["counts"]["placed"], 1)
        self.assertEqual(len(result["buckets"]), 1)
        self.assertIn("14-day window", result["assignments"][0]["note"])

    def test_only_absent_cpu_telemetry_uses_no_data_lane(self):
        result = self.build([
            vm("unmonitored", "Standard_D4as_v5", noData=True, windowDays=None)
        ])
        self.assertEqual(result["counts"]["noData"], 1)
        self.assertEqual(result["assignments"][0]["bucketKey"], "__nodata__")

    def test_tagged_decommission_risk_uses_flexible_savings_plan(self):
        result = self.build([
            vm(
                "project-vm",
                "Standard_E2as_v5",
                decommissionRisk="environment=development",
            )
        ])
        self.assertEqual(
            result["assignments"][0]["bucketKey"], "__savingsplan__"
        )
        self.assertIn("environment=development", result["assignments"][0]["note"])

    def test_savings_plan_uses_reconciled_retail_rate(self):
        result = self.build(
            [vm(
                "project-vm",
                "Standard_E2as_v5",
                decommissionRisk="environment=development",
            )],
            retail_prices={
                ("westus3", "standard_e2as_v5", "linux"): {
                    "monthly_price": 100.0,
                    "monthly_sp_1y": 72.0,
                }
            },
        )
        assignment = result["assignments"][0]
        self.assertEqual(assignment["refMonthlyPayg"], 100.0)
        self.assertEqual(assignment["refMonthlyCommitment"], 72.0)
        self.assertEqual(assignment["refMonthlySavings"], 28.0)
        self.assertEqual(
            assignment["economicsStatus"], "modeled-retail-reconciled"
        )

    def test_generic_technical_review_stays_on_demand(self):
        result = self.build([
            vm("memory-risk", "Standard_E4as_v5", action="rightsizing_review")
        ])
        self.assertEqual(result["assignments"][0]["bucketKey"], "__review__")
        self.assertEqual(result["counts"]["savingsPlan"], 0)


class ProposalPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "proposal.duckdb")
        self.database.init()

    def tearDown(self):
        self.temp.cleanup()

    def create_flux_board(self):
        return self.database.replace_flux_proposal_board(
            board_name=FLUX_BOARD_NAME,
            description="Generated",
            actor=FLUX_ACTOR,
            buckets=[{
                "region": "westus3",
                "sku": "Standard_D4as_v5",
                "strategy": "1-year reservation",
                "refQuantity": 2,
                "refMonthlyPayg": 200,
                "refMonthlyRi1y": 150,
                "refMonthlySavings": 50,
                "refReservationCheck": "Azure recommendation reconciled",
                "note": "Governed proposal",
            }],
            assignments=[],
            summary_note="Proposal refreshed",
        )

    def test_flux_board_is_read_only_but_copy_is_editable(self):
        board_id = self.create_flux_board()
        with self.assertRaisesRegex(PermissionError, "read-only"):
            self.database.rename_rightsizing_board(board_id, "Changed")
        with self.assertRaisesRegex(PermissionError, "read-only"):
            self.database.set_primary_rightsizing_board(board_id)
        with self.assertRaisesRegex(PermissionError, "read-only"):
            self.database.delete_rightsizing_board(board_id)

        copied = self.database.duplicate_rightsizing_board(
            board_id, "My approved plan", actor="planner@example.com"
        )
        renamed = self.database.rename_rightsizing_board(
            copied["id"], "FY27 approved plan"
        )
        self.assertEqual(renamed["name"], "FY27 approved plan")

    def test_generated_economics_are_persisted(self):
        board_id = self.create_flux_board()
        board = self.database.rightsizing_plan_board(board_id)
        bucket = board["buckets"][0]
        self.assertEqual(bucket["refMonthlyRi1y"], 150.0)
        self.assertEqual(bucket["refMonthlySavings"], 50.0)
        self.assertEqual(
            bucket["refReservationCheck"], "Azure recommendation reconciled"
        )


class ProposalCadenceTests(unittest.TestCase):
    def test_due_after_three_days(self):
        refreshed = datetime(2026, 8, 1, 4, 0, tzinfo=timezone.utc)

        class Database:
            def rightsizing_boards(self):
                return [{
                    "name": FLUX_BOARD_NAME,
                    "createdBy": FLUX_ACTOR,
                    "updatedAt": refreshed.isoformat(),
                }]

        self.assertFalse(
            proposal_status(Database(), now=refreshed + timedelta(hours=71))["due"]
        )
        self.assertTrue(
            proposal_status(Database(), now=refreshed + timedelta(hours=72))["due"]
        )


if __name__ == "__main__":
    unittest.main()
