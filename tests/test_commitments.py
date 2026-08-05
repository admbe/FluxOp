from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.commitments import normalize_recommendation, normalize_reservation
from api.database import FluxDatabase
from api.semantic_layer import SemanticQuery


RESERVATION_ITEM = {
    "id": (
        "/providers/microsoft.capacity/reservationOrders/order-1"
        "/reservations/res-1"
    ),
    "name": "res-1",
    "location": "westus3",
    "sku": {"name": "Standard_D2as_v5"},
    "properties": {
        "displayName": "Prod compute block",
        "reservedResourceType": "VirtualMachines",
        "quantity": 10,
        "term": "P1Y",
        "appliedScopeType": "Shared",
        "provisioningState": "Succeeded",
        "expiryDate": "2027-03-01",
        "utilization": {
            "aggregates": [
                {"grain": 1.0, "value": 62.5},
                {"grain": 7.0, "value": 71.0},
                {"grain": 30.0, "value": 68.4},
            ]
        },
    },
}

RECOMMENDATION_ITEM = {
    "id": "/subscriptions/sub-1/providers/x/reservationRecommendations/r1",
    "location": "westus3",
    "properties": {
        "scope": "Single",
        "resourceType": "virtualmachines",
        "skuName": "Standard_E2as_v7",
        "term": "P1Y",
        "lookBackPeriod": "Last30Days",
        "recommendedQuantity": 4,
        "costWithNoReservedInstances": 12000.0,
        "totalCostWithReservedInstances": 8400.0,
        "netSavings": 3600.0,
    },
}


class CommitmentNormalizationTests(unittest.TestCase):
    def test_reservation_normalizes_utilization_and_identity(self):
        row = normalize_reservation(RESERVATION_ITEM)
        self.assertEqual(row["orderId"], "order-1")
        self.assertEqual(row["sku"], "Standard_D2as_v5")
        self.assertEqual(row["quantity"], 10)
        self.assertEqual(row["utilization7d"], 71.0)
        self.assertEqual(row["utilization30d"], 68.4)
        self.assertEqual(row["expiryDate"], "2027-03-01")

    def test_reservation_tolerates_missing_fields(self):
        row = normalize_reservation({"id": "x"})
        self.assertEqual(row["quantity"], 0)
        self.assertIsNone(row["utilization30d"])
        self.assertIsNone(row["expiryDate"])

    def test_recommendation_normalizes_costs(self):
        row = normalize_recommendation(RECOMMENDATION_ITEM, "sub-1", "Prod")
        self.assertEqual(row["sku"], "Standard_E2as_v7")
        self.assertEqual(row["netSavings"], 3600.0)
        self.assertEqual(row["recommendedQuantity"], 4.0)
        self.assertEqual(row["subscriptionName"], "Prod")


class CommitmentStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "c.duckdb")
        self.database.init()

    def tearDown(self):
        self.temp.cleanup()

    def _store(self, snapshot: str, utilization: float) -> None:
        reservation = dict(
            normalize_reservation(RESERVATION_ITEM), utilization30d=utilization
        )
        self.database.store_commitments(
            snapshot,
            [reservation],
            [normalize_recommendation(RECOMMENDATION_ITEM, "sub-1", "Prod")],
        )

    def test_current_views_follow_latest_snapshot(self):
        self._store("run-1", 50.0)
        self._store("run-2", 90.0)
        with self.database.connect(read_only=True) as db:
            rows = db.execute(
                "SELECT utilization_30d FROM reservation_inventory_current"
            ).fetchall()
        self.assertEqual([row[0] for row in rows], [90.0])

    def test_partial_run_preserves_other_feed(self):
        self._store("run-1", 50.0)
        # A later run where inventory was denied but recommendations
        # returned must not blank the reservation view.
        self.database.store_commitments(
            "run-2",
            [],
            [normalize_recommendation(RECOMMENDATION_ITEM, "sub-1", "Prod")],
        )
        with self.database.connect(read_only=True) as db:
            reservations = db.execute(
                "SELECT COUNT(*) FROM reservation_inventory_current"
            ).fetchone()[0]
            recommendations = db.execute(
                "SELECT COUNT(*) FROM reservation_recommendations_current"
            ).fetchone()[0]
        self.assertEqual(reservations, 1)
        self.assertEqual(recommendations, 1)

    def test_semantic_models_answer_utilization_questions(self):
        self._store("run-1", 68.4)
        utilization = self.database.run_semantic_query(
            SemanticQuery(
                model="commitments",
                measures=(
                    "reservation_count",
                    "average_utilization_30d",
                    "underused_reservations",
                    "expiring_within_90d",
                ),
            )
        )
        row = utilization["rows"][0]
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], 68.4)
        self.assertEqual(row[2], 1, "68.4% is under the 80% threshold")

        recommendations = self.database.run_semantic_query(
            SemanticQuery(
                model="commitment_recommendations",
                measures=("net_savings", "recommended_quantity"),
                dimensions=("subscription_name",),
            )
        )
        self.assertEqual(recommendations["rows"][0][1], 3600.0)


if __name__ == "__main__":
    unittest.main()
