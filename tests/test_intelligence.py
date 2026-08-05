import unittest

from api.intelligence import flux_intelligence_queries, normalize_flux_intelligence


class FluxIntelligenceTests(unittest.TestCase):
    def test_required_tag_query_is_configurable_and_sanitized(self):
        query = flux_intelligence_queries(
            30,
            ("owner", "cost-center", "bad' value"),
            ("microsoft.insights/metricalerts",),
        )[0]
        self.assertIn("tags['owner']", query)
        self.assertIn("tags['cost-center']", query)
        self.assertNotIn("bad' value", query)
        self.assertIn("microsoft.insights/metricalerts", query)

    def test_normalizes_and_deduplicates_findings(self):
        raw = {
            "ruleId": "stopped_allocated_vm",
            "resourceId": "/subscriptions/SUB-1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-1",
            "subscriptionId": "SUB-1",
            "resourceGroup": "rg",
            "region": "eastus2",
            "resourceType": "Microsoft.Compute/virtualMachines",
            "resourceName": "vm-1",
            "powerState": "PowerState/stopped",
        }

        findings = normalize_flux_intelligence(
            [raw, raw],
            [{"subscriptionId": "sub-1", "label": "Production"}],
        )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["source"], "flux_intelligence")
        self.assertEqual(findings[0]["subscriptionName"], "Production")
        self.assertEqual(findings[0]["impact"], "High")
        self.assertEqual(findings[0]["confidence"], "High")
        self.assertIn("Stopped but allocated VM", findings[0]["title"])
        self.assertIsNone(findings[0]["estimatedMonthlySavings"])

    def test_ignores_unknown_rules(self):
        findings = normalize_flux_intelligence(
            [{"ruleId": "unknown", "resourceId": "/resource/one"}],
            [],
        )

        self.assertEqual(findings, [])

    def test_toolkit_ahb_rules_are_optional_and_attributed(self):
        enabled = flux_intelligence_queries(finops_toolkit_ahb_enabled=True)
        disabled = flux_intelligence_queries(finops_toolkit_ahb_enabled=False)
        self.assertEqual(len(enabled), len(disabled) + 2)
        self.assertIn("windows_ahb_eligibility_review", enabled[-2])

        finding = normalize_flux_intelligence(
            [{
                "ruleId": "windows_ahb_eligibility_review",
                "resourceId": "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.Compute/virtualMachines/vm-1",
                "subscriptionId": "sub",
                "resourceName": "vm-1",
            }],
            [{"subscriptionId": "sub", "label": "Production"}],
        )[0]
        self.assertEqual(finding["confidence"], "Review")
        self.assertEqual(
            finding["evidence"]["upstream"]["project"],
            "Microsoft FinOps Toolkit",
        )
        self.assertEqual(finding["evidence"]["upstream"]["version"], "v14")
        self.assertEqual(finding["evidence"]["upstream"]["license"], "MIT")

    def test_retirement_rule_carries_official_reference(self):
        findings = normalize_flux_intelligence(
            [
                {
                    "ruleId": "basic_public_ip_retired",
                    "resourceId": "/subscriptions/sub-1/publicIps/ip-1",
                    "resourceName": "ip-1",
                    "resourceType": "microsoft.network/publicipaddresses",
                    "subscriptionId": "sub-1",
                    "skuName": "Basic",
                }
            ],
            [{"subscriptionId": "sub-1", "label": "Production"}],
        )

        retirement = findings[0]["evidence"]["retirement"]
        self.assertEqual(retirement["date"], "2025-09-30")
        self.assertEqual(retirement["source"], "Microsoft Learn")


if __name__ == "__main__":
    unittest.main()
