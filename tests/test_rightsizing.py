from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase
from api.rightsizing import assess_resource


class RightsizingTests(unittest.TestCase):
    def setUp(self):
        self.end = datetime.now(timezone.utc)
        self.start = self.end - timedelta(days=14)

    def assess(self, **overrides):
        values = {
            "attempt_status": "covered",
            "window_start": self.start,
            "window_end": self.end,
            "cpu_p95": 3,
            "cpu_maximum": 10,
            "cpu_coverage": 100,
            "memory_p95": 40,
            "network_in_p95": 1_000_000,
            "network_out_p95": 2_000_000,
        }
        values.update(overrides)
        return assess_resource(**values)

    def test_missing_telemetry_is_not_idle(self):
        result = self.assess(
            attempt_status="no_data",
            cpu_p95=None,
            cpu_maximum=None,
            cpu_coverage=None,
            network_in_p95=None,
            network_out_p95=None,
        )
        self.assertEqual(result["status"], "insufficient_telemetry")
        self.assertEqual(result["coverageFlag"], "none")

    def test_complete_low_use_window_yields_shutdown_candidate(self):
        result = self.assess()
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["kind"], "shutdown")
        self.assertEqual(result["targetSku"], "Deallocate VM")

    def test_high_peak_prevents_idle_classification(self):
        result = self.assess(cpu_maximum=80)
        self.assertNotEqual(result["kind"], "shutdown")
        self.assertEqual(result["status"], "target_rate_unavailable")

    def test_advisor_target_plus_low_cpu_yields_resize_candidate(self):
        result = self.assess(
            cpu_p95=20,
            cpu_maximum=75,
            advisor_target_sku="Standard_D2s_v5",
            advisor_monthly_savings=50,
        )
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["kind"], "resize")
        self.assertEqual(result["targetSku"], "Standard_D2s_v5")
        self.assertIn("CPU p95 is 20.0%", result["reason"])
        self.assertIn("30.0% resize-review limit", result["reason"])
        self.assertIn("14 days", result["reason"])
        self.assertIn("100.0% CPU coverage", result["reason"])

    def test_source_disagreement_requires_review(self):
        result = self.assess(source_disagreement=True)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["coverageFlag"], "partial")

    def test_missing_memory_prevents_action(self):
        result = self.assess(memory_p95=None)
        self.assertEqual(result["status"], "partial_telemetry")

    def test_high_memory_prevents_action(self):
        result = self.assess(memory_p95=91)
        self.assertEqual(result["status"], "no_opportunity")

    def test_database_persists_idle_evidence_and_cost_value(self):
        with TemporaryDirectory() as temp:
            database = FluxDatabase(Path(temp) / "rightsizing.duckdb")
            database.init()
            resource_id = (
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "microsoft.compute/virtualmachines/vm-1"
            )
            database.store_snapshot(
                "snapshot-1",
                [
                    {
                        "resourceId": resource_id,
                        "name": "vm-1",
                        "resourceType": "microsoft.compute/virtualmachines",
                        "subscriptionId": "sub",
                        "subscriptionName": "Production",
                        "resourceGroup": "rg",
                        "region": "eastus2",
                        "sku": "Standard_D4s_v5",
                    }
                ],
                costs=[
                    {
                        "periodStart": "2026-07-01",
                        "periodEnd": "2026-07-10",
                        "costType": "AmortizedCost",
                        "subscriptionId": "sub",
                        "resourceId": resource_id,
                        "amount": 100,
                        "currency": "USD",
                    }
                ],
                cost_scopes=[("sub", "AmortizedCost")],
            )
            run_id = database.start_telemetry_run("azure_monitor", "test")
            summaries = []
            for metric, p95, maximum in (
                ("Percentage CPU", 3, 10),
                ("Memory Used Percentage", 40, 55),
                ("Network In Total", 1_000_000, 2_000_000),
                ("Network Out Total", 2_000_000, 3_000_000),
            ):
                summaries.append(
                    {
                        "resourceId": resource_id,
                        "source": "azure_monitor",
                        "metric": metric,
                        "unit": "Percent" if metric == "Percentage CPU" else "Bytes",
                        "windowStart": self.start,
                        "windowEnd": self.end,
                        "sampleCount": 336,
                        "coveragePercent": 100,
                        "average": p95 / 2,
                        "p95": p95,
                        "maximum": maximum,
                        "lastValue": p95,
                        "lastObservedAt": self.end,
                    }
                )
            database.store_telemetry_summaries(run_id, summaries)
            database.store_telemetry_attempts(
                run_id,
                [
                    {
                        "resourceId": resource_id,
                        "source": "azure_monitor",
                        "status": "covered",
                        "metricCount": 3,
                        "message": "covered",
                    }
                ],
            )
            database.finish_telemetry_run(run_id, "succeeded", 1, "ok")

            count = database.compute_rightsizing_recommendations(run_id)
            result = database.rightsizing_recommendations(status="candidate")
            item = result["items"][0]

            self.assertEqual(count, 1)
            self.assertEqual(item["kind"], "shutdown")
            self.assertEqual(item["coverageFlag"], "covered")
            self.assertEqual(item["estimatedMonthlySaving"], 310.0)
            self.assertEqual(item["valueSource"], "amortized_cost_run_rate")
            self.assertFalse(item["evidence"]["logicMonitorMetricsUsed"])
            detail = database.resource_telemetry(resource_id)
            self.assertEqual(
                detail["rightsizingAssessment"]["status"],
                "candidate",
            )
            self.assertIn(
                "CPU p95 is 3.0%",
                detail["rightsizingAssessment"]["reason"],
            )


if __name__ == "__main__":
    unittest.main()
