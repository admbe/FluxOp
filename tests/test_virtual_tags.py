from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase
from api.virtual_tags import effective_tags, rule_matches, validate_rule


RESOURCE = {
    "resourceId": "/subscriptions/sub-1/rg/x/vm-hceaos004",
    "name": "HCEAOS004",
    "subscriptionId": "sub-1",
    "resourceGroup": "prod-erp-westeu-rg",
    "resourceType": "microsoft.compute/virtualmachines",
    "region": "westeurope",
    "tags": {"env": "Production"},
}


class RuleEvaluationTests(unittest.TestCase):
    def test_matching_is_case_insensitive_and_anded(self):
        conditions = {
            "regions": ["WestEurope"],
            "nameContains": ["hceaos"],
            "tagEquals": {"ENV": ["production"]},
        }
        self.assertTrue(rule_matches(conditions, RESOURCE))
        conditions["regions"] = ["eastus"]
        self.assertFalse(rule_matches(conditions, RESOURCE))

    def test_name_patterns_use_globs(self):
        self.assertTrue(
            rule_matches({"namePatterns": ["hce*004"]}, RESOURCE)
        )
        self.assertFalse(
            rule_matches({"namePatterns": ["prd-*"]}, RESOURCE)
        )

    def test_unknown_condition_keys_fail_closed(self):
        self.assertFalse(rule_matches({"subscription": ["sub-1"]}, RESOURCE))

    def test_generalized_and_or_operators(self):
        self.assertTrue(
            rule_matches(
                {
                    "combinator": "and",
                    "conditions": [
                        {"field": "name", "operator": "starts_with", "value": "hce"},
                        {"field": "nativeTag", "key": "env", "operator": "in", "values": ["Production", "QA"]},
                    ],
                },
                RESOURCE,
            )
        )
        self.assertTrue(
            rule_matches(
                {
                    "combinator": "or",
                    "conditions": [
                        {"field": "region", "operator": "equals", "value": "eastus"},
                        {"field": "resourceGroup", "operator": "contains", "value": "erp"},
                    ],
                },
                RESOURCE,
            )
        )

    def test_exclusion_removes_only_rule_assignment(self):
        rules = [
            {"ruleId": "include", "name": "include", "tagKey": "region", "tagValue": "EU", "effect": "include", "priority": 100, "status": "active", "conditions": {"regions": ["westeurope"]}},
            {"ruleId": "exclude", "name": "exclude", "tagKey": "region", "tagValue": "", "effect": "exclude", "priority": 10, "status": "active", "conditions": {"nameContains": ["hceaos"]}},
        ]
        self.assertNotIn("region", effective_tags(RESOURCE, rules, [], date(2026, 8, 3)))

    def test_precedence_override_beats_rule_beats_native(self):
        rules = [
            {
                "ruleId": "r-weak", "name": "weak", "tagKey": "department",
                "tagValue": "Infra-Weak", "priority": 200, "status": "active",
                "conditions": {"regions": ["westeurope"]},
            },
            {
                "ruleId": "r-strong", "name": "strong", "tagKey": "department",
                "tagValue": "ERP", "priority": 10, "status": "active",
                "conditions": {"nameContains": ["hceaos"]},
            },
            {
                "ruleId": "r-env", "name": "env", "tagKey": "env",
                "tagValue": "RuleEnv", "priority": 50, "status": "active",
                "conditions": {"regions": ["westeurope"]},
            },
        ]
        overrides = [
            {"tagKey": "owner", "tagValue": "Hannes Garber", "source": "imported"},
            {"tagKey": "owner", "tagValue": "Adam Bruncaj", "source": "manual"},
        ]
        resolved = effective_tags(RESOURCE, rules, overrides, date(2026, 8, 3))
        self.assertEqual(resolved["department"]["value"], "ERP")
        self.assertEqual(resolved["department"]["source"], "rule")
        self.assertEqual(resolved["env"]["value"], "RuleEnv")
        self.assertEqual(resolved["owner"]["value"], "Adam Bruncaj")
        self.assertEqual(resolved["owner"]["source"], "manual")

    def test_effective_dates_gate_rules(self):
        rule = {
            "ruleId": "r1", "name": "future", "tagKey": "wave",
            "tagValue": "9", "priority": 10, "status": "active",
            "conditions": {"regions": ["westeurope"]},
            "effectiveFrom": "2027-01-01",
        }
        resolved = effective_tags(RESOURCE, [rule], [], date(2026, 8, 3))
        self.assertNotIn("wave", resolved)

    def test_validate_rule_reports_problems(self):
        problems = validate_rule({"tagKey": "bad key!", "conditions": {}})
        self.assertTrue(any("name" in item for item in problems))
        self.assertTrue(any("tagKey" in item for item in problems))
        self.assertTrue(any("condition" in item for item in problems))


class RulePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "vtags.duckdb")
        self.database.init()

    def tearDown(self):
        self.temp.cleanup()

    def test_rule_lifecycle_versions_and_audits(self):
        created = self.database.save_virtual_tag_rule(
            {
                "name": "ERP department",
                "tagKey": "department",
                "tagValue": "ERP",
                "priority": 10,
                "conditions": {"nameContains": ["hceaos"]},
            },
            actor="tester",
        )
        self.assertEqual(created["version"], 1)
        updated = self.database.save_virtual_tag_rule(
            {
                "ruleId": created["ruleId"],
                "name": "ERP department",
                "tagKey": "department",
                "tagValue": "ERP-EU",
                "priority": 10,
                "conditions": {"nameContains": ["hceaos"]},
            },
            actor="tester",
        )
        self.assertEqual(updated["version"], 2)
        self.database.set_virtual_tag_rule_status(
            created["ruleId"], "inactive", "tester"
        )
        rules = self.database.virtual_tag_rules()
        self.assertEqual(rules[0]["status"], "inactive")
        self.assertEqual(rules[0]["version"], 3)
        with self.database.operational_connect(read_only=True) as db:
            audit_count = db.execute(
                "SELECT count(*) FROM virtual_tag_rule_audit "
                "WHERE rule_id = ?",
                [created["ruleId"]],
            ).fetchone()[0]
        self.assertEqual(audit_count, 3)

    def test_dimension_lifecycle_and_legacy_discovery(self):
        saved = self.database.save_virtual_tag_dimension(
            {"key": "BusinessRegion", "name": "Business region", "description": "Showback axis"},
            actor="tester",
        )
        self.assertEqual(saved["version"], 1)
        dimensions = self.database.virtual_tag_dimensions()
        self.assertEqual(dimensions[0]["key"], "BusinessRegion")
        self.database.delete_virtual_tag_dimension("BusinessRegion", "tester")
        self.assertEqual(self.database.virtual_tag_dimensions()[0]["status"], "inactive")

    def test_empty_virtual_tag_report_is_explicit_and_stable(self):
        self.database.save_virtual_tag_dimension(
            {"key": "CostCenter", "name": "Cost center"}, actor="tester"
        )
        report = self.database.virtual_tag_report(dimension="CostCenter")
        self.assertEqual(report["dimension"], "CostCenter")
        self.assertEqual(report["summary"]["totalCost"], 0)
        self.assertEqual(report["values"], [])
        self.assertIn("current inventory", report["lineage"]["limitation"])

    def test_override_import_returns_rollback_state(self):
        first = self.database.import_virtual_tag_overrides(
            [
                {
                    "resourceId": "/subscriptions/s/r/vm-1",
                    "tagKey": "application",
                    "tagValue": "MS Axapta (ERP)",
                    "source": "imported",
                }
            ],
            actor="tester",
        )
        self.assertEqual(first["applied"], 1)
        self.assertIsNone(first["previous"][0]["previousValue"])
        second = self.database.import_virtual_tag_overrides(
            [
                {
                    "resourceId": "/subscriptions/s/r/vm-1",
                    "tagKey": "application",
                    "tagValue": "Axapta",
                    "source": "manual",
                }
            ],
            actor="tester",
        )
        self.assertEqual(
            second["previous"][0]["previousValue"], "MS Axapta (ERP)"
        )

    def test_override_rollback_restores_and_deletes_with_concurrency_guard(self):
        self.database.import_virtual_tag_overrides(
            [
                {
                    "resourceId": "/subscriptions/s/r/vm-1",
                    "tagKey": "department",
                    "tagValue": "ERP",
                    "source": "imported",
                },
                {
                    "resourceId": "/subscriptions/s/r/vm-1",
                    "tagKey": "environment",
                    "tagValue": "Production",
                    "source": "imported",
                },
            ],
            actor="tester",
        )
        result = self.database.rollback_virtual_tag_overrides(
            [
                {
                    "resourceId": "/subscriptions/s/r/vm-1",
                    "tagKey": "department",
                    "previousValue": "Finance",
                    "previousSource": "manual",
                    "expectedValue": "ERP",
                },
                {
                    "resourceId": "/subscriptions/s/r/vm-1",
                    "tagKey": "environment",
                    "previousValue": None,
                    "previousSource": None,
                    "expectedValue": "Production",
                },
            ],
            actor="tester",
        )
        self.assertEqual(result, {"restored": 2, "skipped": 0, "conflicts": 0})
        tags = self.database.virtual_tag_overrides_for(
            ["/subscriptions/s/r/vm-1"]
        )["/subscriptions/s/r/vm-1"]
        self.assertEqual(tags[0]["tagValue"], "Finance")
        self.assertEqual({item["tagKey"] for item in tags}, {"department"})

        self.database.import_virtual_tag_overrides(
            [
                {
                    "resourceId": "/subscriptions/s/r/vm-1",
                    "tagKey": "department",
                    "tagValue": "Newer",
                    "source": "manual",
                }
            ],
            actor="newer-editor",
        )
        conflict = self.database.rollback_virtual_tag_overrides(
            [
                {
                    "resourceId": "/subscriptions/s/r/vm-1",
                    "tagKey": "department",
                    "previousValue": "Old",
                    "expectedValue": "Finance",
                }
            ],
            actor="tester",
        )
        self.assertEqual(conflict["conflicts"], 1)


if __name__ == "__main__":
    unittest.main()
