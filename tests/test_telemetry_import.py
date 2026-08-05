from pathlib import Path
from tempfile import TemporaryDirectory
import csv
import json
import unittest

from api.database import FluxDatabase
from api.telemetry_import import import_bootstrap


RESOURCE_ID = (
    "/subscriptions/sub/resourceGroups/rg/providers/"
    "Microsoft.Compute/virtualMachines/vm-1"
)


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class TelemetryImportTests(unittest.TestCase):
    def test_import_is_idempotent_and_preserves_source_disagreement(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            lm = root / "logicmonitor"
            azure = root / "azure-monitor"
            lm.mkdir()
            azure.mkdir()
            run_summary = {
                "StartedUtc": "2026-07-22T00:00:00Z",
                "CompletedUtc": "2026-07-22T01:00:00Z",
                "TimeGrain": "PT1H",
            }
            (lm / "Run-Summary.json").write_text(json.dumps(run_summary))
            (azure / "Run-Summary.json").write_text(json.dumps(run_summary))
            write_csv(
                lm / "LogicMonitor-Azure-Matches.csv",
                [{
                    "LogicMonitorId": "42",
                    "LogicMonitorDisplayName": "vm-1",
                    "AzureResourceId": RESOURCE_ID,
                    "MatchStatus": "matched",
                    "MatchMethod": "azure_resource_id",
                    "LogicMonitorPlatform": "Windows",
                }],
            )
            base = {
                "CollectionEndUtc": "2026-07-22T01:00:00Z",
                "RequestedHistoryDays": "30",
                "CPUSampleCount": "720",
                "CPUCoveragePercent": "100",
                "MemorySampleCount": "720",
                "MemoryUsedP95Percent": "40",
                "NetworkInMbpsP95": "1",
                "NetworkOutMbpsP95": "1",
            }
            write_csv(
                lm / "VM-Metric-Coverage.csv",
                [{
                    "LogicMonitorId": "42",
                    "DiskNetHistoryDays": "14",
                    "CPUAveragePercent": "10",
                    "CPUP95Percent": "12",
                    "CPUMaxPercent": "20",
                    **base,
                }],
            )
            write_csv(
                azure / "VM-Metric-Coverage.csv",
                [{
                    "AzureResourceId": RESOURCE_ID,
                    "CPUAveragePercent": "35",
                    "CPUP95Percent": "45",
                    "CPUMaxPercent": "60",
                    **base,
                }],
            )

            database = FluxDatabase(root / "test.duckdb")
            database.init()
            database.store_snapshot(
                "inventory",
                [{
                    "resourceId": RESOURCE_ID,
                    "name": "vm-1",
                    "resourceType": "microsoft.compute/virtualmachines",
                    "subscriptionId": "sub",
                    "subscriptionName": "Production",
                    "resourceGroup": "rg",
                    "region": "eastus",
                    "sku": "Standard_D4s_v5",
                }],
            )

            first = import_bootstrap(database, lm, azure)
            second = import_bootstrap(database, lm, azure)
            telemetry = database.resource_telemetry(RESOURCE_ID)
            recommendations = database.rightsizing_recommendations()

            self.assertEqual(first["logicMonitorResources"], 1)
            self.assertEqual(first["azureMonitorResources"], 1)
            self.assertEqual(first, second)
            self.assertEqual({item["source"] for item in telemetry["metrics"]},
                             {"azure_monitor", "logicmonitor"})
            self.assertTrue(all(item["aggregationMethod"]
                                for item in telemetry["metrics"]))
            self.assertEqual(recommendations["items"][0]["status"], "needs_review")
            self.assertEqual(recommendations["summary"]["needsReview"], 1)
            self.assertGreater(
                recommendations["items"][0]["evidence"]["cpuP95Delta"], 20
            )


if __name__ == "__main__":
    unittest.main()
