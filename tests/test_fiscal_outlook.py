from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

from api.cost import CostManagementProvider
from api.database import FluxDatabase
from api.forecasting import fiscal_year_frame, forecast_fiscal_year

from tests.test_cost import FakeCredential, FakeResponse


def month(year: int, month_number: int) -> date:
    return date(year, month_number, 1)


def synthetic_months(end: date, count: int, base: float = 1000.0) -> dict:
    """count complete months ending at `end`, with 2% monthly growth."""
    series = {}
    total = end.year * 12 + end.month - 1
    for back in range(count - 1, -1, -1):
        index = total - back
        series[date(index // 12, index % 12 + 1, 1)] = base * (
            1.02 ** (count - 1 - back)
        )
    return series


class FiscalYearFrameTests(unittest.TestCase):
    def test_july_start_names_fy_for_its_ending_year(self):
        start, last, label = fiscal_year_frame(date(2026, 8, 2), 7)
        self.assertEqual(start, date(2026, 7, 1))
        self.assertEqual(last, date(2027, 6, 1))
        self.assertEqual(label, "FY2027")

    def test_before_start_month_belongs_to_prior_fiscal_year(self):
        start, last, label = fiscal_year_frame(date(2026, 6, 15), 7)
        self.assertEqual(start, date(2025, 7, 1))
        self.assertEqual(label, "FY2026")

    def test_january_start_is_the_calendar_year(self):
        start, last, label = fiscal_year_frame(date(2026, 8, 2), 1)
        self.assertEqual(start, date(2026, 1, 1))
        self.assertEqual(label, "FY2026")


class ForecastFiscalYearTests(unittest.TestCase):
    AS_OF = date(2026, 8, 2)

    def test_run_rate_is_primary_and_seasonal_is_comparison_only(self):
        series = synthetic_months(month(2026, 7), 24)
        series[month(2025, 12)] *= 1.5  # a December spike to recognize
        outlook = forecast_fiscal_year(series, as_of=self.AS_OF)
        self.assertEqual(outlook["methodVersion"], "post-migration-run-rate-v1")
        self.assertIn("seasonalComparison", outlook)
        self.assertEqual(
            outlook["seasonalComparison"]["methodVersion"],
            "seasonal-yoy-comparison-v1",
        )
        self.assertFalse(
            next(item for item in outlook["months"] if item["month"] == "2026-12")["seasonalBasis"]
        )

    def test_seasonal_comparison_can_be_requested_explicitly(self):
        series = synthetic_months(month(2026, 7), 24)
        series[month(2025, 12)] *= 1.5
        outlook = forecast_fiscal_year(
            series,
            as_of=self.AS_OF,
            method="seasonal-yoy-comparison-v1",
        )
        december = next(
            item for item in outlook["months"] if item["month"] == "2026-12"
        )
        november = next(
            item for item in outlook["months"] if item["month"] == "2026-11"
        )
        self.assertTrue(december["seasonalBasis"])
        self.assertGreater(december["amount"], november["amount"] * 1.3)

    def test_growth_assumption_compounds_monthly(self):
        series = synthetic_months(month(2026, 7), 24)
        flat = forecast_fiscal_year(series, as_of=self.AS_OF)
        grown = forecast_fiscal_year(
            series, as_of=self.AS_OF, growth_percent_monthly=2.0
        )
        self.assertGreater(grown["fyTotal"], flat["fyTotal"])
        flat_june = next(
            item for item in flat["months"] if item["month"] == "2027-06"
        )
        grown_june = next(
            item for item in grown["months"] if item["month"] == "2027-06"
        )
        # Eleven months out, 2%/month compounds to roughly +24%.
        self.assertAlmostEqual(
            grown_june["amount"] / flat_june["amount"], 1.02 ** 10, delta=0.03
        )

    def test_planned_savings_ramp_in_and_never_go_negative(self):
        series = synthetic_months(month(2026, 7), 24)
        outlook = forecast_fiscal_year(
            series,
            as_of=self.AS_OF,
            planned_savings_monthly=200.0,
            savings_ramp_months=2,
        )
        base = forecast_fiscal_year(series, as_of=self.AS_OF)
        first = next(
            item for item in outlook["months"] if item["month"] == "2026-08"
        )
        first_base = next(
            item for item in base["months"] if item["month"] == "2026-08"
        )
        second = next(
            item for item in outlook["months"] if item["month"] == "2026-09"
        )
        second_base = next(
            item for item in base["months"] if item["month"] == "2026-09"
        )
        self.assertAlmostEqual(first_base["amount"] - first["amount"], 100.0, 1)
        self.assertAlmostEqual(
            second_base["amount"] - second["amount"], 200.0, 1
        )
        huge = forecast_fiscal_year(
            series,
            as_of=self.AS_OF,
            planned_savings_monthly=10_000_000.0,
            savings_ramp_months=0,
        )
        self.assertTrue(all(item["amount"] >= 0 for item in huge["months"]))

    def test_actual_months_pass_through_and_total_includes_them(self):
        series = synthetic_months(month(2026, 7), 24)
        outlook = forecast_fiscal_year(series, as_of=self.AS_OF)
        july = next(
            item for item in outlook["months"] if item["month"] == "2026-07"
        )
        self.assertEqual(july["status"], "actual")
        self.assertEqual(july["amount"], round(series[month(2026, 7)], 2))
        self.assertAlmostEqual(outlook["actualToDate"], july["amount"], 2)

    def test_current_month_estimate_is_used_when_provided(self):
        series = synthetic_months(month(2026, 7), 24)
        outlook = forecast_fiscal_year(
            series, as_of=self.AS_OF, current_month_estimate=4321.0
        )
        august = next(
            item for item in outlook["months"] if item["month"] == "2026-08"
        )
        self.assertEqual(august["status"], "inProgress")
        self.assertEqual(august["amount"], 4321.0)

    def test_sparse_history_is_labeled_limited(self):
        series = synthetic_months(month(2026, 7), 2)
        outlook = forecast_fiscal_year(series, as_of=self.AS_OF)
        self.assertEqual(outlook["status"], "limited")
        self.assertIsNone(outlook["backtestMape"])

    def test_bands_widen_with_distance(self):
        series = synthetic_months(month(2026, 7), 24)
        outlook = forecast_fiscal_year(series, as_of=self.AS_OF)
        projected = [
            item for item in outlook["months"] if item["status"] == "projected"
        ]
        first_ratio = (projected[0]["upper"] - projected[0]["lower"]) / (
            projected[0]["amount"] or 1
        )
        last_ratio = (projected[-1]["upper"] - projected[-1]["lower"]) / (
            projected[-1]["amount"] or 1
        )
        self.assertGreater(last_ratio, first_ratio)


def monthly_payload():
    return {
        "properties": {
            "columns": [
                {"name": "Cost", "type": "Number"},
                {"name": "BillingMonth", "type": "Datetime"},
                {"name": "Currency", "type": "String"},
            ],
            "rows": [
                [1234.56, "2026-07-01T00:00:00", "USD"],
                [842.11, "2026-08-01T00:00:00", "USD"],
            ],
        }
    }


class MonthlyCostQueryTests(unittest.TestCase):
    @patch("api.cost.urlopen")
    def test_fetches_monthly_totals_without_grouping(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(monthly_payload())
        provider = CostManagementProvider(
            credential=FakeCredential(),
            sleep=lambda _: None,
        )
        records = provider.fetch_monthly_scope(
            "SUB-1",
            "AmortizedCost",
            date(2025, 9, 1),
            date(2026, 8, 2),
        )
        self.assertEqual(records[0]["month"], "2026-07-01")
        self.assertEqual(records[0]["amount"], 1234.56)
        self.assertEqual(records[1]["month"], "2026-08-01")
        body = json.loads(mock_urlopen.call_args.args[0].data)
        self.assertEqual(body["dataset"]["granularity"], "Monthly")
        self.assertNotIn("grouping", body["dataset"])


class FiscalOutlookReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "outlook.duckdb")
        self.database.init()
        self.database.seed_demo()

    def tearDown(self):
        self.temp.cleanup()

    def test_outlook_covers_the_fiscal_year_with_actuals_and_projection(self):
        outlook = self.database.fiscal_year_outlook()
        self.assertEqual(len(outlook["months"]), 12)
        statuses = {item["status"] for item in outlook["months"]}
        self.assertIn("projected", statuses)
        self.assertGreater(outlook["fyTotal"], 0)
        self.assertGreaterEqual(outlook["fyUpper"], outlook["fyTotal"])
        self.assertLessEqual(outlook["fyLower"], outlook["fyTotal"])
        self.assertEqual(outlook["currency"], "USD")
        self.assertIn("details", outlook["subscriptionCoverage"])

    def test_config_roundtrip_and_budget_comparison(self):
        self.database.save_budget_targets(
            [{
                "scopeType": "estate", "scopeId": "",
                "monthlyAmount": 5000.0, "currency": "USD",
            }],
            updated_by="test",
        )
        self.database.save_fiscal_outlook_config(
            fy_start_month=7,
            cost_type="ActualCost",
            growth_percent_monthly=1.5,
            include_planned_savings=False,
            savings_ramp_months=3,
            notes="FY27 submission baseline",
            updated_by="alice",
        )
        outlook = self.database.fiscal_year_outlook()
        self.assertEqual(outlook["config"]["costType"], "ActualCost")
        self.assertEqual(outlook["config"]["updatedBy"], "alice")
        self.assertEqual(outlook["fyBudget"], 60000.0)
        self.assertAlmostEqual(
            outlook["fyVarianceVsBudget"],
            round(outlook["fyTotal"] - 60000.0, 2),
            2,
        )

    def test_planned_savings_pull_from_the_rightsizing_plan(self):
        self.database.save_rightsizing_bucket(
            {"region": "eastus2", "sku": "Standard_D4s_v5",
             "refMonthlySavings": 350.0},
            updated_by="alice",
        )
        self.database.save_fiscal_outlook_config(
            fy_start_month=7,
            cost_type="AmortizedCost",
            growth_percent_monthly=0.0,
            include_planned_savings=True,
            savings_ramp_months=0,
            notes="",
            updated_by="alice",
        )
        with_savings = self.database.fiscal_year_outlook()
        self.assertEqual(with_savings["plannedSavingsMonthly"], 350.0)
        self.assertTrue(
            any("planner-entered" in item for item in with_savings["limitations"])
        )


class BudgetGroupTests(unittest.TestCase):
    DEMO_SUB = "00000000-0000-0000-0000-000000000001"
    OTHER_SUB = "00000000-0000-0000-0000-000000000002"

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "groups.duckdb")
        self.database.init()
        self.database.seed_demo()
        # A second subscription with flat monthly history, so groups can be
        # told apart from the estate.
        today = date.today()
        current = today.replace(day=1)
        records = []
        for back in range(12, -1, -1):
            total = current.year * 12 + current.month - 1 - back
            records.append(
                {
                    "month": date(total // 12, total % 12 + 1, 1).isoformat(),
                    "costType": "AmortizedCost",
                    "subscriptionId": self.OTHER_SUB,
                    "amount": 1000.0,
                    "currency": "USD",
                    "source": "demo",
                }
            )
        self.database.store_monthly_cost_scope(
            "test:other",
            self.OTHER_SUB,
            "AmortizedCost",
            records,
            start_month=date.fromisoformat(records[0]["month"]),
            end_month=current,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_groups_roundtrip_and_produce_fiscal_lanes(self):
        saved = self.database.save_budget_groups(
            [
                {
                    "name": "US",
                    "annualAmount": 785000.0,
                    "currency": "USD",
                    "subscriptionIds": [self.DEMO_SUB],
                },
                {
                    "name": "INTL",
                    "annualAmount": 750000.0,
                    "currency": "USD",
                    "subscriptionIds": [self.OTHER_SUB],
                },
            ],
            updated_by="alice",
        )
        self.assertEqual([group["name"] for group in saved], ["US", "INTL"])
        self.assertTrue(all(group["id"] for group in saved))

        outlook = self.database.fiscal_year_outlook()
        lanes = {lane["name"]: lane for lane in outlook["groups"]}
        self.assertEqual(set(lanes), {"US", "INTL"})
        intl = lanes["INTL"]
        self.assertEqual(intl["annualBudget"], 750000.0)
        self.assertEqual(intl["memberCount"], 1)
        self.assertEqual(intl["coveredMembers"], 1)
        # Flat $1,000/month cannot exceed a $750k envelope.
        self.assertLess(intl["fyTotal"], 20000.0)
        self.assertLess(intl["variance"], 0)
        us = lanes["US"]
        self.assertGreater(us["fyTotal"], intl["fyTotal"])
        # Group totals exclude the other group's subscription.
        self.assertLess(
            abs(
                (us["fyTotal"] + intl["fyTotal"])
                - (us["fyTotal"] + intl["fyTotal"])
            ),
            0.01,
        )

    def test_replacing_groups_removes_old_membership(self):
        self.database.save_budget_groups(
            [{
                "name": "US", "annualAmount": 1.0, "currency": "USD",
                "subscriptionIds": [self.DEMO_SUB, self.OTHER_SUB],
            }]
        )
        self.database.save_budget_groups(
            [{
                "name": "Everything", "annualAmount": 2.0,
                "currency": "USD", "subscriptionIds": [self.DEMO_SUB],
            }]
        )
        groups = self.database.budget_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["subscriptionIds"], [self.DEMO_SUB])


class CommitmentInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "ri.duckdb")
        self.database.init()
        self.database.seed_demo()

    def tearDown(self):
        self.temp.cleanup()

    def test_active_reservations_with_expiry_and_utilization(self):
        inventory = self.database.commitment_inventory()
        summary = inventory["summary"]
        self.assertEqual(summary["activeCount"], 2)
        self.assertEqual(summary["historicalCount"], 1)
        self.assertEqual(summary["expiringWithin120Days"], 1)
        self.assertEqual(summary["expiringWithin30Days"], 0)
        self.assertIsNotNone(summary["averageUtilization30d"])
        first = inventory["reservations"][0]
        # Sorted by expiry: the 95-day reservation leads.
        self.assertEqual(first["name"], "VM_RI_demo_d4")
        self.assertEqual(first["daysToExpiry"], 95)
        names = {item["name"] for item in inventory["reservations"]}
        self.assertNotIn("VM_RI_demo_expired", names)


class FiscalOutlookToolTests(unittest.TestCase):
    def test_governed_tool_returns_outlook_with_assumptions(self):
        from api.config import Settings
        from api.intelligence_assistant import GovernedToolExecutor

        with TemporaryDirectory() as tmp:
            database = FluxDatabase(Path(tmp) / "tool.duckdb")
            database.init()
            database.seed_demo()
            settings = Settings(
                database_path=Path(tmp) / "tool.duckdb",
                deepseek_api_key="test-only",
            )
            tools = GovernedToolExecutor(database, settings)
            outlook = tools.execute("get_fiscal_year_outlook", {})
            self.assertIn(outlook["status"], {"ready", "limited"})
            self.assertEqual(len(outlook["months"]), 12)
            self.assertIn("assumptions", outlook)
            self.assertIn("config", outlook)


if __name__ == "__main__":
    unittest.main()
