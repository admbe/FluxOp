from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase
from api.drift import anomaly_result, classify_changes


def resource(
    name: str,
    *,
    sku: str = "Standard_D2s_v5",
    tags: dict[str, str] | None = None,
) -> dict:
    resource_id = (
        "/subscriptions/sub/resourceGroups/rg/providers/"
        f"microsoft.compute/virtualmachines/{name}"
    )
    return {
        "resourceId": resource_id,
        "name": name,
        "resourceType": "microsoft.compute/virtualmachines",
        "subscriptionId": "sub",
        "subscriptionName": "Production",
        "resourceGroup": "rg",
        "region": "eastus2",
        "kind": "",
        "sku": sku,
        "managedBy": "",
        "tags": tags or {},
    }


class DriftTests(unittest.TestCase):
    def test_exact_diff_classifies_create_delete_resize_and_retag(self):
        deleted = resource("deleted")
        changed_before = resource("changed", tags={"owner": "one"})
        changed_after = resource(
            "changed",
            sku="Standard_D4s_v5",
            tags={"owner": "two"},
        )
        created = resource("created")

        changes = classify_changes(
            {
                deleted["resourceId"].lower(): deleted,
                changed_before["resourceId"].lower(): changed_before,
            },
            {
                changed_after["resourceId"].lower(): changed_after,
                created["resourceId"].lower(): created,
            },
        )

        self.assertEqual(
            {item["changeType"] for item in changes},
            {"created", "deleted", "resized", "retagged"},
        )

    def test_median_mad_flags_burst_and_suppresses_warmup(self):
        warming = anomaly_result(
            20,
            [1, 1],
            minimum_points=5,
            threshold_k=3,
        )
        burst = anomaly_result(
            20,
            [1, 1, 1, 1, 1],
            minimum_points=5,
            threshold_k=3,
        )
        normal = anomaly_result(
            1,
            [1, 1, 1, 1, 1],
            minimum_points=5,
            threshold_k=3,
        )

        self.assertEqual(warming["status"], "warming_up")
        self.assertEqual(burst["status"], "anomalous")
        self.assertFalse(normal["isAnomaly"])

    def test_database_exposes_latest_snapshot_changes(self):
        with TemporaryDirectory() as temp:
            database = FluxDatabase(Path(temp) / "drift.duckdb")
            database.init()
            first = [resource("deleted"), resource("changed")]
            second = [
                resource("changed", sku="Standard_D4s_v5"),
                resource("created"),
            ]
            database.store_snapshot("snapshot-1", first)
            self.assertEqual(database.compute_inventory_drift("snapshot-1"), 0)
            database.store_snapshot("snapshot-2", second)

            count = database.compute_inventory_drift(
                "snapshot-2",
                minimum_points=5,
                threshold_k=3,
            )
            result = database.changes()
            anomalies = database.change_anomalies()

            self.assertEqual(count, 3)
            self.assertEqual(result["total"], 3)
            self.assertEqual(result["summary"]["created"], 1)
            self.assertEqual(result["summary"]["deleted"], 1)
            self.assertEqual(result["summary"]["configuration"], 1)
            self.assertTrue(anomalies["warmingUp"])
            self.assertTrue(
                all(item["status"] == "warming_up" for item in anomalies["items"])
            )


if __name__ == "__main__":
    unittest.main()
