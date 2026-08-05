"""Evidence for the Alpha-feature maturity moves: Changes pagination and
filters, anomaly evaluator edge behavior, and a native policy contract test
mirroring the Rill-side validation."""

from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.cost_anomaly import evaluate_cost_series
from api.database import FluxDatabase
from api.semantic_layer import SemanticQuery


def resource(name, *, sku="Standard_D2s_v5", tags=None):
    return {
        "resourceId": (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            f"microsoft.compute/virtualmachines/{name}"
        ),
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


class ChangesHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "changes.duckdb")
        self.database.init()
        first = [resource(f"vm-{index}") for index in range(6)]
        second = (
            # 3 resized, 2 deleted, 2 created.
            [resource(f"vm-{index}", sku="Standard_D4s_v5") for index in range(3)]
            + [resource(f"vm-{index}") for index in range(3, 4)]
            + [resource("vm-new-a"), resource("vm-new-b")]
        )
        self.database.store_snapshot("snap-1", first)
        self.database.compute_inventory_drift("snap-1")
        self.database.store_snapshot("snap-2", second)
        self.database.compute_inventory_drift("snap-2")

    def tearDown(self):
        self.temp.cleanup()

    def test_pagination_is_stable_and_non_overlapping(self):
        everything = self.database.changes(limit=250, offset=0)
        self.assertGreaterEqual(everything["total"], 7)
        page_one = self.database.changes(limit=3, offset=0)
        page_two = self.database.changes(limit=3, offset=3)
        self.assertEqual(page_one["total"], everything["total"])
        ids = lambda result: [
            (item["resourceId"], item["changeType"])
            for item in result["items"]
        ]
        self.assertEqual(len(ids(page_one)), 3)
        self.assertFalse(set(ids(page_one)) & set(ids(page_two)))

    def test_change_type_filter_and_facets(self):
        created = self.database.changes(change_type="created")
        self.assertEqual(created["total"], 2)
        self.assertTrue(
            all(item["changeType"] == "created" for item in created["items"])
        )
        # Facets stay unfiltered so the dropdowns never collapse.
        self.assertIn("created", created["facets"]["changeTypes"])
        self.assertIn("deleted", created["facets"]["changeTypes"])

    def test_search_matches_resource_name(self):
        result = self.database.changes(search="vm-new-a")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["resourceName"], "vm-new-a")

    def test_empty_result_keeps_contract_shape(self):
        result = self.database.changes(search="no-such-resource-anywhere")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["items"], [])
        self.assertIn("changeTypes", result["facets"])
        self.assertIn("summary", result)


class AnomalyEvaluatorEdgeTests(unittest.TestCase):
    evaluation_date = date(2026, 6, 15)

    def _series(self, baseline_value, current, weeks=9):
        amounts = {}
        for offset in range(1, weeks + 1):
            amounts[self.evaluation_date - timedelta(days=7 * offset)] = (
                baseline_value
            )
        # Fill the surrounding days so history is mature.
        for day_offset in range(1, 64):
            day = self.evaluation_date - timedelta(days=day_offset)
            amounts.setdefault(day, baseline_value)
        amounts[self.evaluation_date] = current
        return amounts

    def test_minimum_increase_suppresses_tiny_absolute_spikes(self):
        # 300% jump but only +6 in absolute terms: noise, not an incident.
        result = evaluate_cost_series(
            self._series(2.0, 8.0), self.evaluation_date
        )
        self.assertEqual(result["status"], "normal")
        self.assertEqual(result["severity"], "none")

    def test_zero_mad_with_real_increase_is_high_severity(self):
        # A perfectly flat baseline (MAD 0) jumping by 150 is the textbook
        # step change: anomalous, high.
        result = evaluate_cost_series(
            self._series(100.0, 250.0), self.evaluation_date
        )
        self.assertEqual(result["status"], "anomalous")
        self.assertEqual(result["severity"], "high")
        self.assertEqual(result["baselineMedian"], 100.0)

    def test_percent_floor_keeps_large_bases_quiet(self):
        # +30 on a 1000 base clears minimum_increase but is 3%: normal.
        result = evaluate_cost_series(
            self._series(1000.0, 1030.0), self.evaluation_date
        )
        self.assertEqual(result["status"], "normal")

    def test_missing_recent_days_read_as_zero_not_error(self):
        amounts = self._series(100.0, 250.0)
        # Drop the last two weekday-matching baseline points entirely;
        # the evaluator documents missing days as zero spend.
        amounts.pop(self.evaluation_date - timedelta(days=7))
        amounts.pop(self.evaluation_date - timedelta(days=14))
        result = evaluate_cost_series(amounts, self.evaluation_date)
        self.assertIn(result["status"], {"anomalous", "normal"})
        self.assertIsNotNone(result["baselineMedian"])


class PolicyContractTests(unittest.TestCase):
    """The in-app policy numbers and the semantic governance model must
    agree -- the same guarantee the Rill contract test gives, natively."""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "policy.duckdb")
        self.database.init()
        self.database.store_policy_posture(
            "policy-snap",
            [
                {
                    "subscriptionId": "sub-1",
                    "subscriptionName": "Production",
                    "assignmentId": "/assignments/secure",
                    "assignmentName": "Secure baseline",
                    "evaluatedCount": 10,
                    "compliantCount": 7,
                    "nonCompliantCount": 2,
                    "exemptCount": 1,
                    "unknownCount": 0,
                    "resourceCount": 8,
                    "definitionCount": 3,
                },
                {
                    "subscriptionId": "sub-2",
                    "subscriptionName": "Development",
                    "assignmentId": "/assignments/tags",
                    "assignmentName": "Required tags",
                    "evaluatedCount": 20,
                    "compliantCount": 11,
                    "nonCompliantCount": 9,
                    "exemptCount": 0,
                    "unknownCount": 0,
                    "resourceCount": 15,
                    "definitionCount": 1,
                },
            ],
        )
        self.database.refresh_current_views()

    def tearDown(self):
        self.temp.cleanup()

    def test_policy_report_matches_semantic_governance_model(self):
        native = self.database.policy_report()
        semantic = self.database.run_semantic_query(
            SemanticQuery(
                model="governance",
                measures=(
                    "evaluated",
                    "compliant",
                    "non_compliant",
                    "compliance_percent",
                ),
            )
        )
        row = semantic["rows"][0]
        by_name = dict(
            zip([column["name"] for column in semantic["columns"]], row)
        )
        summary = native["summary"]
        self.assertEqual(by_name["evaluated"], summary["evaluated"])
        self.assertEqual(by_name["compliant"], summary["compliant"])
        self.assertEqual(by_name["non_compliant"], summary["nonCompliant"])
        expected_percent = round(
            summary["compliant"] / summary["evaluated"] * 100, 1
        )
        self.assertAlmostEqual(
            round(float(by_name["compliance_percent"]), 1), expected_percent
        )

    def test_subscription_filter_agrees_between_layers(self):
        native = self.database.policy_report(subscription_id="sub-2")
        semantic = self.database.run_semantic_query(
            SemanticQuery(
                model="governance",
                measures=("evaluated", "non_compliant"),
                filters={"subscription_name": ("Development",)},
            )
        )
        by_name = dict(
            zip(
                [column["name"] for column in semantic["columns"]],
                semantic["rows"][0],
            )
        )
        self.assertEqual(by_name["evaluated"], native["summary"]["evaluated"])
        self.assertEqual(
            by_name["non_compliant"], native["summary"]["nonCompliant"]
        )
