import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from api.cost_anomaly import evaluate_cost_series
from api.database import FluxDatabase


class CostAnomalyEvaluationTests(unittest.TestCase):
    def test_requires_mature_history(self):
        evaluation_date = date(2026, 7, 20)
        result = evaluate_cost_series(
            {
                evaluation_date - timedelta(days=7): 10,
                evaluation_date: 100,
            },
            evaluation_date,
        )

        self.assertEqual(result["status"], "warming_up")
        self.assertEqual(result["baselinePoints"], 1)

    def test_detects_matching_weekday_spike(self):
        evaluation_date = date(2026, 7, 20)
        amounts = {
            evaluation_date - timedelta(days=7 * week): 100 + (week % 2)
            for week in range(1, 9)
        }
        amounts[evaluation_date] = 350

        result = evaluate_cost_series(amounts, evaluation_date)

        self.assertEqual(result["status"], "anomalous")
        self.assertEqual(result["severity"], "high")
        self.assertGreater(result["absoluteChange"], 200)

    def test_keeps_normal_seasonal_cost_quiet(self):
        evaluation_date = date(2026, 7, 20)
        amounts = {
            evaluation_date - timedelta(days=7 * week): 100 + (week % 3)
            for week in range(1, 9)
        }
        amounts[evaluation_date] = 103

        result = evaluate_cost_series(amounts, evaluation_date)

        self.assertEqual(result["status"], "normal")


class CostAnomalyDatabaseTests(unittest.TestCase):
    def test_successful_refresh_replaces_complete_scope_window(self):
        with tempfile.TemporaryDirectory() as temp:
            database = FluxDatabase(Path(temp) / "cost-refresh.duckdb")
            database.init()
            observed = date(2026, 7, 20)
            stale = {
                "usageDate": observed.isoformat(),
                "resourceId": "/subscriptions/sub-1/resources/stale",
                "serviceName": "Old service",
                "amount": 10,
                "currency": "USD",
            }
            current = {
                "usageDate": observed.isoformat(),
                "resourceId": "/subscriptions/sub-1/resources/current",
                "serviceName": "Current service",
                "amount": 20,
                "currency": "USD",
            }
            database.store_daily_cost_scope(
                "first",
                "sub-1",
                "ActualCost",
                [stale],
            )

            database.store_daily_cost_scope(
                "refresh",
                "sub-1",
                "ActualCost",
                [current],
                start_date=observed,
                end_date=observed,
            )

            with database.connect(read_only=True) as db:
                rows = db.execute(
                    """
                    SELECT resource_id
                    FROM daily_cost_history
                    ORDER BY resource_id
                    """
                ).fetchall()
            self.assertEqual(
                rows,
                [("/subscriptions/sub-1/resources/current",)],
            )

    def test_publishes_subscription_service_and_resource_anomalies(self):
        with tempfile.TemporaryDirectory() as temp:
            database = FluxDatabase(Path(temp) / "cost-anomaly.duckdb")
            database.init()
            evaluation_date = date(2026, 7, 20)
            resource_id = (
                "/subscriptions/sub-1/resourcegroups/rg/providers/"
                "microsoft.compute/virtualmachines/vm-1"
            )
            records = [
                {
                    "usageDate": (
                        evaluation_date - timedelta(days=7 * week)
                    ).isoformat(),
                    "resourceId": resource_id,
                    "serviceName": "Virtual Machines",
                    "amount": 100 + (week % 2),
                    "currency": "USD",
                }
                for week in range(1, 9)
            ]
            records.append(
                {
                    "usageDate": evaluation_date.isoformat(),
                    "resourceId": resource_id,
                    "serviceName": "Virtual Machines",
                    "amount": 350,
                    "currency": "USD",
                }
            )
            database.store_daily_cost_scope(
                "daily-test",
                "sub-1",
                "AmortizedCost",
                records,
            )

            result = database.compute_cost_anomalies(
                "anomaly-test",
                latency_days=0,
                minimum_history_days=28,
                minimum_baseline_points=4,
                baseline_weeks=8,
                threshold_k=3.5,
                minimum_increase=10,
                as_of=evaluation_date,
            )
            published = database.cost_anomalies(
                cost_type="AmortizedCost",
                status="anomalous",
            )

            self.assertEqual(result["anomalyCount"], 3)
            self.assertEqual(published["total"], 3)
            self.assertEqual(
                {item["scopeType"] for item in published["items"]},
                {"subscription", "service", "resource"},
            )
            self.assertEqual(published["summary"]["currency"], "USD")


if __name__ == "__main__":
    unittest.main()
