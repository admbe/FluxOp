"""Estate telemetry coverage accounting over a synthetic mixed estate."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase


def vm(resource_id, name, monthly_cost):
    return {
        "resourceId": resource_id,
        "name": name,
        "resourceType": "microsoft.compute/virtualmachines",
        "subscriptionId": "sub",
        "subscriptionName": "Production",
        "resourceGroup": "rg",
        "region": "westus3",
        "estimatedMonthlyCost": monthly_cost,
    }


class TelemetryCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "cov.duckdb")
        self.database.init()
        self.database.store_snapshot(
            "estate",
            [
                vm("/subscriptions/sub/r/vm-cheap", "vm-cheap", 50.0),
                vm("/subscriptions/sub/r/vm-mid", "vm-mid", 300.0),
                vm("/subscriptions/sub/r/vm-big", "vm-big", 900.0),
                vm("/subscriptions/sub/r/vm-thin", "vm-thin", 120.0),
                {
                    "resourceId": "/subscriptions/sub/r/disk-1",
                    "name": "disk-1",
                    "resourceType": "microsoft.compute/disks",
                    "subscriptionId": "sub",
                    "estimatedMonthlyCost": 999.0,
                },
            ],
        )
        run_id = self.database.start_telemetry_run("azure_monitor", "test")
        self.database.store_telemetry_summaries(
            run_id,
            [
                self._summary(
                    "/subscriptions/sub/r/vm-cheap", "azure_monitor", 100.0
                ),
                self._summary(
                    "/subscriptions/sub/r/vm-thin", "azure_monitor", 30.0
                ),
            ],
        )
        self.database.finish_telemetry_run(run_id, "succeeded", 2, "ok")
        lm_run = self.database.start_telemetry_run("logicmonitor", "test")
        self.database.store_telemetry_summaries(
            lm_run,
            [
                self._summary(
                    "/subscriptions/sub/r/vm-mid", "logicmonitor", 95.0
                ),
            ],
        )
        self.database.finish_telemetry_run(lm_run, "succeeded", 1, "ok")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _summary(resource_id, source, coverage):
        return {
            "resourceId": resource_id,
            "source": source,
            "metric": "Percentage CPU",
            "unit": "Percent",
            "windowStart": "2026-07-10T00:00:00Z",
            "windowEnd": "2026-07-24T00:00:00Z",
            "sampleCount": 336,
            "coveragePercent": coverage,
            "average": 12.5,
            "p95": 31.0,
            "maximum": 88.0,
            "lastValue": 10.0,
            "lastObservedAt": "2026-07-24T00:00:00Z",
        }

    def test_estate_accounting_and_spend_ranked_uncovered(self):
        report = self.database.telemetry_coverage_report()
        # Only VMs count; the expensive disk must not inflate anything.
        self.assertEqual(report["totalVms"], 4)
        self.assertEqual(report["coveredVms"], 3)
        self.assertEqual(report["coveredPercent"], 75.0)
        # vm-big (900/month) is the only uncovered VM -- and the headline
        # number the rollout conversation needs.
        self.assertEqual(len(report["uncovered"]), 1)
        self.assertEqual(report["uncovered"][0]["name"], "vm-big")
        self.assertEqual(report["uncoveredMonthlyCost"], 900.0)
        self.assertEqual(report["totalMonthlyCost"], 1370.0)
        self.assertEqual(report["coveredMonthlyCost"], 470.0)
        # vm-thin is covered but below the evidence threshold.
        self.assertEqual(report["lowCoverageVms"], 1)
        by_source = {
            item["source"]: item for item in report["bySource"]
        }
        self.assertEqual(by_source["azure_monitor"]["vmCount"], 2)
        self.assertEqual(by_source["logicmonitor"]["vmCount"], 1)
        self.assertEqual(
            by_source["azure_monitor"]["averageCoveragePercent"], 65.0
        )
        self.assertFalse(report["uncoveredTruncated"])

    def test_uncovered_ranking_is_by_monthly_cost(self):
        # Wipe telemetry: everything is uncovered; order must be spend-desc.
        with self.database.connect() as db:
            db.execute("DELETE FROM telemetry_metric_summaries")
        self.database.refresh_current_views()
        report = self.database.telemetry_coverage_report(uncovered_limit=3)
        names = [item["name"] for item in report["uncovered"]]
        self.assertEqual(names, ["vm-big", "vm-mid", "vm-thin"])
        self.assertTrue(report["uncoveredTruncated"])
        self.assertEqual(report["coveredVms"], 0)
        self.assertEqual(report["coveredPercent"], 0.0)

    def test_empty_estate_reports_none_not_division_error(self):
        empty = FluxDatabase(Path(self.temp.name) / "empty.duckdb")
        empty.init()
        report = empty.telemetry_coverage_report()
        self.assertEqual(report["totalVms"], 0)
        self.assertIsNone(report["coveredPercent"])
        self.assertEqual(report["uncovered"], [])
