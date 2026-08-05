from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.confidence import confidence_score
from api.database import FluxDatabase


def finding(resource_id: str, rule_id: str = "unattached_disk") -> dict:
    return {
        "findingId": f"{rule_id}:{resource_id}",
        "ruleId": rule_id,
        "source": "flux_intelligence",
        "resourceId": resource_id,
        "relatedResourceId": "",
        "subscriptionId": "sub",
        "subscriptionName": "Production",
        "resourceType": "microsoft.compute/disks",
        "resourceGroup": "rg",
        "region": "eastus2",
        "category": "Cost",
        "impact": "Medium",
        "confidence": "High",
        "title": "Unattached disk",
        "reason": "The disk is unattached.",
        "evidence": {},
        "estimatedMonthlySavings": None,
        "savingsCurrency": "",
        "ruleVersion": "test",
    }


class ConfidenceTests(unittest.TestCase):
    def test_duckdb_decimal_evidence_is_normalized(self):
        now = datetime.now(timezone.utc)
        result = confidence_score(
            family="unattached_disk",
            consecutive_count=1,
            source_count=1,
            source_evidence=Decimal("0.9"),
            last_seen=now,
            computed_at=now,
        )
        self.assertIsInstance(result["score"], float)
        self.assertEqual(result["factors"]["sourceEvidence"], 0.9)

    def test_telemetry_changes_utilization_dependent_score(self):
        now = datetime.now(timezone.utc)
        covered = confidence_score(
            family="compute_shutdown",
            consecutive_count=2,
            source_count=1,
            source_evidence=0.9,
            last_seen=now,
            computed_at=now,
            telemetry_status="covered",
        )
        missing = confidence_score(
            family="compute_shutdown",
            consecutive_count=2,
            source_count=1,
            source_evidence=0.9,
            last_seen=now,
            computed_at=now,
            telemetry_status="",
        )
        self.assertGreater(covered["score"], missing["score"])
        self.assertTrue(covered["telemetryApplicable"])

    def test_freshness_reduces_score(self):
        now = datetime.now(timezone.utc)
        fresh = confidence_score(
            family="unattached_disk",
            consecutive_count=1,
            source_count=1,
            source_evidence=1.0,
            last_seen=now,
            computed_at=now,
        )
        stale = confidence_score(
            family="unattached_disk",
            consecutive_count=1,
            source_count=1,
            source_evidence=1.0,
            last_seen=now - timedelta(days=45),
            computed_at=now,
        )
        self.assertGreater(fresh["score"], stale["score"])

    def test_persistence_and_reappearance_are_stored(self):
        with TemporaryDirectory() as temp:
            database = FluxDatabase(Path(temp) / "confidence.duckdb")
            database.init()
            resource_a = (
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "microsoft.compute/disks/disk-a"
            )
            resource_b = (
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "microsoft.compute/disks/disk-b"
            )
            resources = [
                {
                    "resourceId": resource_id,
                    "name": resource_id.rsplit("/", 1)[-1],
                    "resourceType": "microsoft.compute/disks",
                    "subscriptionId": "sub",
                    "subscriptionName": "Production",
                }
                for resource_id in (resource_a, resource_b)
            ]

            database.store_snapshot(
                "snapshot-1",
                resources,
                advisor_collected=True,
                intelligence=[finding(resource_a)],
                intelligence_collected=True,
            )
            database.compute_opportunity_confidence("snapshot-1")
            database.store_snapshot(
                "snapshot-2",
                resources,
                advisor_collected=True,
                intelligence=[finding(resource_a), finding(resource_b)],
                intelligence_collected=True,
            )
            database.compute_opportunity_confidence("snapshot-2")

            current = {
                item["resourceId"]: item
                for item in database.opportunities(include_governance=True)["items"]
            }
            resource_a_key = resource_a.lower()
            resource_b_key = resource_b.lower()
            self.assertEqual(current[resource_a_key]["consecutiveCount"], 2)
            self.assertEqual(current[resource_b_key]["consecutiveCount"], 1)
            self.assertGreater(
                current[resource_a_key]["confidenceScore"],
                current[resource_b_key]["confidenceScore"],
            )
            ranked = database.opportunities(
                include_governance=True,
                sort="confidence",
            )["items"]
            self.assertEqual(ranked[0]["resourceId"], resource_a_key)

            database.store_snapshot(
                "snapshot-3",
                resources,
                advisor_collected=True,
                intelligence=[],
                intelligence_collected=True,
            )
            database.compute_opportunity_confidence("snapshot-3")
            database.store_snapshot(
                "snapshot-4",
                resources,
                advisor_collected=True,
                intelligence=[finding(resource_b)],
                intelligence_collected=True,
            )
            database.compute_opportunity_confidence("snapshot-4")

            reappeared = database.opportunities(
                include_governance=True
            )["items"][0]
            self.assertEqual(reappeared["resourceId"], resource_b_key)
            self.assertEqual(reappeared["consecutiveCount"], 1)
            self.assertTrue(reappeared["reappearedAfterRemediation"])
            self.assertEqual(
                reappeared["confidenceMethodVersion"],
                "opportunity-confidence-v1",
            )
            self.assertEqual(database.ensure_opportunity_confidence(), 0)


if __name__ == "__main__":
    unittest.main()
