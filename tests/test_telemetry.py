import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from api.database import FluxDatabase
from api.telemetry import AzureMonitorProvider, LogicMonitorProvider


class TelemetryTests(unittest.TestCase):
    def test_azure_monitor_uses_regional_batch_api(self):
        class Credential:
            scopes = []

            def get_token(self, scope: str):
                self.scopes.append(scope)
                return type("Token", (), {"token": "test"})()

        resource_id = (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "microsoft.compute/virtualMachines/vm-1"
        )
        with patch(
            "api.telemetry._request_json",
            return_value={"values": [{"resourceid": resource_id, "value": []}]},
        ) as request:
            _, attempts = AzureMonitorProvider(
                Credential(), "https://management.azure.com", days=14
            ).fetch(
                [
                    {
                        "resourceId": resource_id,
                        "name": "vm-1",
                        "subscriptionId": "sub",
                        "region": "East US 2",
                    }
                ]
            )
        http_request = request.call_args.args[0]
        url = http_request.full_url
        self.assertEqual(http_request.method, "POST")
        self.assertIn("https://eastus2.metrics.monitor.azure.com/", url)
        self.assertIn("starttime=", url)
        self.assertIn("endtime=", url)
        self.assertNotIn("timespan=", url)
        self.assertEqual(
            json.loads(http_request.data),
            {"resourceids": [resource_id]},
        )
        self.assertEqual(attempts[0]["status"], "no_data")

    def test_ama_log_analytics_summarizes_guest_memory(self):
        from api.telemetry import AmaLogAnalyticsProvider

        class Credential:
            scope = ""

            def get_token(self, scope: str):
                self.scope = scope
                return type("Token", (), {"token": "test"})()

        covered_id = (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "microsoft.compute/virtualmachines/vm-1"
        )
        silent_id = (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "microsoft.compute/virtualmachines/vm-2"
        )
        response = {
            "tables": [
                {
                    "name": "PrimaryResult",
                    "columns": [
                        {"name": "TimeGenerated", "type": "datetime"},
                        {"name": "ResourceId", "type": "string"},
                        {"name": "value", "type": "real"},
                    ],
                    "rows": [
                        ["2026-08-01T10:00:00Z", covered_id, 61.5],
                        ["2026-08-01T11:00:00Z", covered_id, 74.5],
                    ],
                }
            ]
        }
        credential = Credential()
        with patch(
            "api.telemetry._request_json", return_value=response
        ) as request:
            summaries, attempts = AmaLogAnalyticsProvider(
                credential, "workspace-guid", days=14
            ).fetch(
                [
                    {"resourceId": covered_id},
                    {"resourceId": silent_id},
                ]
            )
        self.assertEqual(
            credential.scope, "https://api.loganalytics.io/.default"
        )
        http_request = request.call_args.args[0]
        self.assertIn("/workspaces/workspace-guid/query", http_request.full_url)
        body = json.loads(http_request.data)
        # Locale-robust selection: the percentage counter under the memory
        # object, rather than a list of English counter names that drops
        # German-locale VMs (live incident 2026-08-02).
        self.assertIn('ObjectName in ("Memory", "Arbeitsspeicher")', body["query"])
        self.assertIn('CounterName contains "%"', body["query"])

        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary["metric"], "Memory Used Percentage")
        self.assertEqual(summary["source"], "ama_log_analytics")
        self.assertEqual(summary["sampleCount"], 2)
        self.assertEqual(summary["average"], 68.0)
        self.assertEqual(summary["maximum"], 74.5)

        by_id = {item["resourceId"]: item for item in attempts}
        self.assertEqual(by_id[covered_id.lower()]["status"], "covered")
        self.assertEqual(by_id[silent_id.lower()]["status"], "no_data")
        self.assertIn(
            "agent not reporting", by_id[silent_id.lower()]["message"]
        )

    def test_rightsizing_dossier_assembles_available_evidence(self):
        with TemporaryDirectory() as temp:
            database = FluxDatabase(Path(temp) / "dossier.duckdb")
            database.init()
            resource_id = (
                "/subscriptions/sub-1/resourceGroups/rg/providers/"
                "microsoft.compute/virtualmachines/vm-1"
            ).lower()
            with database.connect() as db:
                db.execute(
                    "INSERT INTO resource_snapshots VALUES ("
                    "'snap-1', now(), ?, 'vm-1', "
                    "'microsoft.compute/virtualmachines', 'sub-1', "
                    "'Production', 'rg', 'westus3', '', 'Standard_D4s_v5', "
                    "'Succeeded', '', '{}', NULL, NULL, NULL, NULL, "
                    "NULL, NULL, NULL, '{}')",
                    [resource_id],
                )
                db.execute(
                    "INSERT INTO source_sync_state VALUES "
                    "('snap-1', now(), 'AzureResourceGraph', "
                    "'configured-subscriptions', 1)"
                )
            database.refresh_current_views()
            dossier = database.rightsizing_dossier(resource_id)
            self.assertEqual(dossier["resource"]["name"], "vm-1")
            self.assertEqual(dossier["resource"]["sku"], "Standard_D4s_v5")
            self.assertEqual(dossier["focusExposure90d"], [])
            self.assertEqual(dossier["retailPriceComparison"], [])
            self.assertIn("metrics", dossier)
            self.assertIn("costDaily", dossier)

    def test_azure_monitor_uses_metrics_token_scope(self):
        class Credential:
            scope = ""

            def get_token(self, scope: str):
                self.scope = scope
                return type("Token", (), {"token": "test"})()

        credential = Credential()
        with patch("api.telemetry._request_json", return_value={"values": []}):
            AzureMonitorProvider(credential, "unused").fetch([])
        self.assertEqual(
            credential.scope, "https://metrics.monitor.azure.com/.default"
        )

    def test_azure_monitor_groups_batches_by_subscription_and_region(self):
        resources = [
            {
                "resourceId": f"/subscriptions/sub-a/resourceGroups/rg/providers/microsoft.compute/virtualMachines/vm-{index}",
                "subscriptionId": "sub-a",
                "region": "eastus",
            }
            for index in range(51)
        ]
        resources.append(
            {
                "resourceId": "/subscriptions/sub-b/resourceGroups/rg/providers/microsoft.compute/virtualMachines/vm-other",
                "subscriptionId": "sub-b",
                "region": "westus3",
            }
        )
        batches = AzureMonitorProvider._batches(resources)
        self.assertEqual([len(batch[2]) for batch in batches], [50, 1, 1])
        self.assertEqual(
            [(batch[0], batch[1]) for batch in batches],
            [("sub-a", "eastus"), ("sub-a", "eastus"), ("sub-b", "westus3")],
        )

    def test_azure_monitor_batch_error_marks_each_resource(self):
        class Credential:
            def get_token(self, _: str):
                return type("Token", (), {"token": "test"})()

        resources = [
            {
                "resourceId": f"/subscriptions/sub/resourceGroups/rg/providers/microsoft.compute/virtualMachines/vm-{index}",
                "subscriptionId": "sub",
                "region": "eastus",
            }
            for index in range(2)
        ]
        with patch("api.telemetry._request_json", side_effect=RuntimeError("batch failed")):
            summaries, attempts = AzureMonitorProvider(
                Credential(), "unused"
            ).fetch(resources)
        self.assertEqual(summaries, [])
        self.assertEqual([item["status"] for item in attempts], ["error", "error"])

    def test_azure_monitor_missing_resource_result_is_error(self):
        class Credential:
            def get_token(self, _: str):
                return type("Token", (), {"token": "test"})()

        with patch("api.telemetry._request_json", return_value={"values": []}):
            _, attempts = AzureMonitorProvider(Credential(), "unused").fetch(
                [
                    {
                        "resourceId": "/subscriptions/sub/resourceGroups/rg/providers/microsoft.compute/virtualMachines/vm-1",
                        "subscriptionId": "sub",
                        "region": "eastus",
                    }
                ]
            )
        self.assertEqual(attempts[0]["status"], "error")
        self.assertIn("no result", attempts[0]["message"])

    def test_azure_monitor_parses_per_resource_batch_results(self):
        class Credential:
            def get_token(self, _: str):
                return type("Token", (), {"token": "test"})()

        resource_id = (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "microsoft.compute/virtualMachines/vm-1"
        )
        payload = {
            "values": [
                {
                    "resourceid": resource_id,
                    "value": [
                        {
                            "name": {"value": "Percentage CPU"},
                            "unit": "Percent",
                            "timeseries": [
                                {
                                    "data": [
                                        {
                                            "timeStamp": "2026-07-24T20:00:00Z",
                                            "average": 12.5,
                                        }
                                    ]
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        with patch("api.telemetry._request_json", return_value=payload):
            summaries, attempts = AzureMonitorProvider(
                Credential(), "unused"
            ).fetch(
                [
                    {
                        "resourceId": resource_id,
                        "subscriptionId": "sub",
                        "region": "eastus",
                    }
                ]
            )
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["metric"], "Percentage CPU")
        self.assertEqual(attempts[0]["status"], "covered")

    def test_logicmonitor_prefers_exact_azure_resource_id(self):
        resource_id = (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "microsoft.compute/virtualMachines/vm-1"
        )
        matches = LogicMonitorProvider.match(
            [
                {
                    "id": 42,
                    "name": "different-host",
                    "displayName": "Different host",
                    "customProperties": [
                        {"name": "AzureResourceId", "value": resource_id}
                    ],
                }
            ],
            [{"resourceId": resource_id, "name": "vm-1"}],
        )
        self.assertEqual(matches[0]["status"], "matched")
        self.assertEqual(matches[0]["method"], "azure_resource_id")
        self.assertEqual(matches[0]["resourceId"], resource_id.lower())

    def test_logicmonitor_collects_normalized_incremental_cpu_samples(self):
        start = datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)

        def response(request, **_):
            url = request.full_url
            if "/data?" in url:
                return {
                    "dataPoints": ["CPUBusyPercent"],
                    "time": [
                        int(start.timestamp() * 1000),
                        int(end.timestamp() * 1000),
                    ],
                    "values": [[10.0], [30.0]],
                }
            if "/instances?" in url:
                return {"items": [{"id": 99, "displayName": "_Total"}]}
            return {
                "items": [
                    {
                        "id": 77,
                        "dataSourceName": "Microsoft_Windows_CPU",
                        "instanceNumber": 1,
                    }
                ]
            }

        provider = LogicMonitorProvider("account", "token", ("5",), 0)
        with patch("api.telemetry._request_json", side_effect=response):
            samples, warnings = provider.fetch_metrics(
                {
                    "sourceResourceId": "42",
                    "resourceId": "/subscriptions/sub/resourceGroups/rg/providers/microsoft.compute/virtualMachines/vm-1",
                    "platform": "Windows",
                },
                start,
                end,
            )

        self.assertEqual(warnings, [])
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0]["metric"], "Percentage CPU")
        self.assertEqual(samples[1]["value"], 30.0)
        self.assertEqual(
            samples[0]["lineage"]["method"],
            "checkpointed_incremental_v1",
        )

    def test_logicmonitor_checkpoint_rotation_and_sample_upsert(self):
        resource_id = (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "microsoft.compute/virtualmachines/vm-1"
        )
        with TemporaryDirectory() as temp:
            database = FluxDatabase(Path(temp) / "incremental.duckdb")
            database.init()
            database.store_snapshot(
                "inventory",
                [
                    {
                        "resourceId": resource_id,
                        "name": "vm-1",
                        "resourceType": "microsoft.compute/virtualmachines",
                        "subscriptionId": "sub",
                    }
                ],
            )
            discovery = database.start_telemetry_run(
                "logicmonitor",
                "test",
            )
            database.store_source_matches(
                discovery,
                [
                    {
                        "source": "logicmonitor",
                        "sourceResourceId": "42",
                        "sourceName": "vm-1",
                        "resourceId": resource_id,
                        "status": "matched",
                        "method": "azure_resource_id",
                        "confidence": "high",
                        "details": {"platform": "Windows"},
                    }
                ],
            )
            database.finish_telemetry_run(
                discovery,
                "succeeded",
                1,
                "matched",
            )
            target = database.logicmonitor_metric_targets(
                10,
                initial_hours=8,
                maximum_window_hours=12,
            )[0]
            run_id = database.start_telemetry_run(
                "logicmonitor",
                "test",
            )
            samples = [
                {
                    "resourceId": resource_id,
                    "source": "logicmonitor",
                    "sourceResourceId": "42",
                    "metric": "Percentage CPU",
                    "unit": "Percent",
                    "observedAt": target["windowStart"],
                    "value": 10.0,
                    "lineage": {},
                },
                {
                    "resourceId": resource_id,
                    "source": "logicmonitor",
                    "sourceResourceId": "42",
                    "metric": "Percentage CPU",
                    "unit": "Percent",
                    "observedAt": target["windowEnd"],
                    "value": 20.0,
                    "lineage": {},
                },
            ]
            database.store_telemetry_samples(run_id, samples)
            database.store_telemetry_samples(run_id, samples)
            database.update_telemetry_checkpoint(
                "42",
                target["windowEnd"],
                status="succeeded",
                message="ok",
            )
            summary_count = database.summarize_logicmonitor_samples(
                run_id,
                [resource_id],
                history_days=14,
            )
            database.finish_telemetry_run(
                run_id,
                "succeeded",
                1,
                "collected",
            )

            with database.connect(read_only=True) as connection:
                sample_count = connection.execute(
                    "SELECT count(*) FROM telemetry_metric_samples"
                ).fetchone()[0]
            status = database.telemetry_status()
            telemetry = database.resource_telemetry(resource_id)

            self.assertEqual(sample_count, 2)
            self.assertEqual(summary_count, 1)
            self.assertEqual(status["logicMonitorCheckpointed"], 1)
            self.assertEqual(status["logicMonitorMetricCovered"], 1)
            self.assertEqual(
                telemetry["metrics"][0]["source"],
                "logicmonitor",
            )

    def test_attempted_vm_does_not_monopolize_next_batch(self):
        with TemporaryDirectory() as temp:
            database = FluxDatabase(Path(temp) / "rotation.duckdb")
            database.init()
            resources = [
                {
                    "resourceId": f"/subscriptions/sub/resourceGroups/rg/providers/microsoft.compute/virtualmachines/vm-{index}",
                    "name": f"vm-{index}",
                    "resourceType": "microsoft.compute/virtualmachines",
                    "subscriptionId": "sub",
                }
                for index in (1, 2)
            ]
            database.store_snapshot("snapshot", resources)
            run_id = database.start_telemetry_run("azure_monitor", "test")
            database.store_telemetry_attempts(
                run_id,
                [
                    {
                        "resourceId": resources[0]["resourceId"],
                        "source": "azure_monitor",
                        "status": "no_data",
                        "metricCount": 0,
                        "message": "No data.",
                    }
                ],
            )
            database.finish_telemetry_run(run_id, "succeeded", 1, "ok")

            self.assertEqual(database.telemetry_targets(1)[0]["name"], "vm-2")

    def test_database_exposes_metric_and_coverage_status(self):
        with TemporaryDirectory() as temp:
            database = FluxDatabase(Path(temp) / "telemetry.duckdb")
            database.init()
            resource_id = (
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "microsoft.compute/virtualmachines/vm-1"
            )
            database.store_snapshot(
                "snapshot",
                [
                    {
                        "resourceId": resource_id,
                        "name": "vm-1",
                        "resourceType": "microsoft.compute/virtualmachines",
                        "subscriptionId": "sub",
                    }
                ],
            )
            run_id = database.start_telemetry_run("azure_monitor", "test")
            database.store_telemetry_summaries(
                run_id,
                [
                    {
                        "resourceId": resource_id,
                        "source": "azure_monitor",
                        "metric": "Percentage CPU",
                        "unit": "Percent",
                        "windowStart": "2026-07-10T00:00:00Z",
                        "windowEnd": "2026-07-24T00:00:00Z",
                        "sampleCount": 336,
                        "coveragePercent": 100,
                        "average": 12.5,
                        "p95": 31.0,
                        "maximum": 88.0,
                        "lastValue": 10.0,
                        "lastObservedAt": "2026-07-24T00:00:00Z",
                    }
                ],
            )
            database.store_telemetry_attempts(
                run_id,
                [
                    {
                        "resourceId": resource_id,
                        "source": "azure_monitor",
                        "status": "covered",
                        "metricCount": 1,
                        "message": "1 metric summary collected.",
                    }
                ],
            )
            database.finish_telemetry_run(run_id, "succeeded", 1, "ok")

            self.assertEqual(database.telemetry_status()["azureMonitorCovered"], 1)
            self.assertEqual(database.telemetry_status()["azureMonitorAttempted"], 1)
            subscription_status = database.telemetry_status()["bySubscription"][0]
            self.assertEqual(subscription_status["subscriptionId"], "sub")
            self.assertEqual(subscription_status["virtualMachines"], 1)
            self.assertEqual(subscription_status["azureMonitorCovered"], 1)
            telemetry = database.resource_telemetry(resource_id)
            self.assertEqual(telemetry["metrics"][0]["p95"], 31.0)
            self.assertEqual(telemetry["azureMonitorAttempt"]["status"], "covered")
            self.assertEqual(
                database.inventory()["items"][0]["utilizationSource"],
                "azure_monitor",
            )
            overview = database.overview()
            self.assertEqual(overview["summary"]["utilizationCoverageCount"], 1)
            self.assertEqual(
                overview["summary"]["averageUtilizationPercent"],
                12.5,
            )
            self.assertEqual(
                overview["utilizationDistribution"],
                [{"name": "Low 5–20%", "value": 1}],
            )
            self.assertEqual(
                overview["telemetryCoverage"],
                [{"name": "Covered", "value": 1}],
            )


if __name__ == "__main__":
    unittest.main()
