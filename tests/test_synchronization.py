from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from api.cost import CostManagementError
from api.database import FluxDatabase
from api.synchronization import execute_azure_sync


def config(**overrides):
    values = {
        "cost_management_enabled": False,
        "cost_management_api_version": "test",
        "cost_management_timeout_seconds": 1,
        "cost_management_max_retries": 0,
        "cost_management_request_delay_seconds": 0,
        "cost_management_throttle_cooldown_seconds": 0,
        "cost_management_qpu_budget_10_seconds": 6,
        "cost_management_qpu_budget_60_seconds": 30,
        "cost_management_qpu_budget_3600_seconds": 300,
        "azure_management_endpoint": "https://management.azure.com",
        "drift_min_baseline_points": 5,
        "drift_mad_threshold": 3.0,
        "backup_storage_account_url": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class SynchronizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "sync.duckdb")
        self.database.init()
        self.integration = {
            "authMode": "managed_identity",
            "subscriptions": [
                {"subscriptionId": "sub-1", "label": "One"},
                {"subscriptionId": "sub-2", "label": "Two"},
            ],
        }

    def tearDown(self):
        self.temp.cleanup()

    @patch("api.synchronization._clients")
    def test_source_specific_inventory_request_does_not_call_enrichments(
        self, clients
    ):
        provider = Mock()
        provider.fetch.return_value = [
            {
                "resourceId": "/subscriptions/sub-1/providers/test/type/item",
                "name": "item",
                "resourceType": "test/type",
                "subscriptionId": "sub-1",
            }
        ]
        clients.return_value = (provider, Mock())
        sync_id = self.database.start_sync(
            "managed_identity", sources=["inventory"]
        )

        execute_azure_sync(
            self.database,
            config(),
            sync_id,
            self.integration,
            ["inventory"],
        )

        provider.fetch.assert_called_once()
        provider.fetch_advisor.assert_not_called()
        provider.fetch_intelligence.assert_not_called()
        latest = self.database.latest_sync()
        self.assertEqual(latest["status"], "succeeded")
        self.assertEqual(latest["sourceRuns"][0]["source"], "AzureResourceGraph")
        self.assertEqual(self.database.inventory()["total"], 1)

    @patch("api.synchronization.CostManagementProvider")
    @patch("api.synchronization._clients")
    def test_cost_scopes_checkpoint_success_and_continue_after_failure(
        self, clients, cost_provider_type
    ):
        clients.return_value = (Mock(), Mock())
        provider = cost_provider_type.return_value
        provider.access_token.return_value = "token"

        def fetch_scope(subscription_id, cost_type, **_):
            if subscription_id == "sub-1" and cost_type == "ActualCost":
                raise CostManagementError("throttled", status_code=429)
            return [
                {
                    "periodStart": "2026-07-01",
                    "periodEnd": "2026-07-25",
                    "costType": cost_type,
                    "subscriptionId": subscription_id,
                    "resourceId": "",
                    "amount": 10,
                    "currency": "USD",
                }
            ]

        provider.fetch_scope.side_effect = fetch_scope
        provider.fetch_commitment_scope.side_effect = lambda subscription_id, **_: [
            {
                "periodStart": "2026-07-01",
                "periodEnd": "2026-07-25",
                "subscriptionId": subscription_id,
                "meterId": "meter-1",
                "pricingModel": "OnDemand",
                "amount": 10,
                "currency": "USD",
            }
        ]
        sync_id = self.database.start_sync(
            "managed_identity", sources=["cost"]
        )

        execute_azure_sync(
            self.database,
            config(cost_management_enabled=True),
            sync_id,
            self.integration,
            ["cost"],
        )

        latest = self.database.latest_sync()
        self.assertEqual(latest["status"], "succeeded")
        self.assertEqual(len(latest["sourceRuns"]), 6)
        self.assertEqual(
            sum(run["status"] == "succeeded" for run in latest["sourceRuns"]),
            5,
        )
        failed = next(
            run for run in latest["sourceRuns"] if run["status"] == "failed"
        )
        self.assertTrue(failed["retainedLastGood"])
        cost_health = {
            item["source"]: item for item in self.database.source_freshness()
        }
        self.assertEqual(cost_health["ActualCost"]["health"], "degraded")
        self.assertEqual(cost_health["CommitmentCoverage"]["health"], "healthy")
        self.assertEqual(
            [call.args[:2] for call in provider.fetch_scope.call_args_list],
            [
                ("sub-1", "ActualCost"),
                ("sub-2", "ActualCost"),
                ("sub-1", "AmortizedCost"),
                ("sub-2", "AmortizedCost"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
