from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase
from api.valuation import monthly_run_rate, value_opportunity


class ValuationTests(unittest.TestCase):
    def test_month_to_date_cost_is_normalized_to_monthly_run_rate(self):
        self.assertEqual(
            monthly_run_rate(
                Decimal("100"),
                date(2026, 7, 1),
                date(2026, 7, 10),
            ),
            310.0,
        )

    def test_non_retirement_rule_is_not_given_full_cost_value(self):
        result = value_opportunity(
            source="flux_intelligence",
            rule_id="missing_allocation_tags",
            advisor_monthly=None,
            advisor_annual=None,
            cost_amount=100,
            cost_type="AmortizedCost",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 10),
            confidence=0.8,
        )
        self.assertEqual(result["status"], "not_valued")
        self.assertIsNone(result["monthlyGross"])

    def test_advisor_annual_estimate_is_normalized(self):
        result = value_opportunity(
            source="azure_advisor",
            rule_id="",
            advisor_monthly=None,
            advisor_annual=Decimal("1200"),
            cost_amount=None,
            cost_type="",
            period_start=None,
            period_end=None,
            confidence=Decimal("0.75"),
        )
        self.assertEqual(result["monthlyGross"], 100.0)
        self.assertEqual(result["monthlyRiskAdjusted"], 75.0)
        self.assertEqual(result["valueSource"], "azure_advisor")

    def test_sku_price_difference_prefers_cost_and_retail_lineage(self):
        result = value_opportunity(
            source="azure_advisor",
            rule_id="",
            advisor_monthly=100,
            advisor_annual=1200,
            cost_amount=100,
            cost_type="AmortizedCost",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 10),
            confidence=Decimal("0.75"),
            current_sku="Standard_D4s_v5",
            target_sku="Standard_D2s_v5",
            target_price_status="matched",
            target_monthly_price=73,
            target_price_currency="USD",
            cost_currency="USD",
        )

        self.assertEqual(result["currentMonthlyCost"], 310.0)
        self.assertEqual(result["targetMonthlyCost"], 73.0)
        self.assertEqual(result["monthlyGross"], 237.0)
        self.assertEqual(result["monthlyRiskAdjusted"], 177.75)
        self.assertEqual(
            result["valueSource"],
            "amortized_cost_minus_retail_target",
        )

    def test_database_persists_cost_provenance_and_risk_adjusted_value(self):
        with TemporaryDirectory() as temp:
            database = FluxDatabase(Path(temp) / "valuation.duckdb")
            database.init()
            resource_id = (
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "microsoft.compute/disks/disk-1"
            )
            database.store_snapshot(
                "snapshot-1",
                [
                    {
                        "resourceId": resource_id,
                        "name": "disk-1",
                        "resourceType": "microsoft.compute/disks",
                        "subscriptionId": "sub",
                        "subscriptionName": "Production",
                        "resourceGroup": "rg",
                        "region": "eastus2",
                    }
                ],
                costs=[
                    {
                        "periodStart": "2026-07-01",
                        "periodEnd": "2026-07-10",
                        "costType": "AmortizedCost",
                        "subscriptionId": "sub",
                        "resourceId": resource_id,
                        "amount": 100,
                        "currency": "USD",
                    }
                ],
                cost_scopes=[("sub", "AmortizedCost")],
                advisor=[],
                advisor_collected=True,
                intelligence=[
                    {
                        "findingId": f"unattached_disk:{resource_id}",
                        "ruleId": "unattached_disk",
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
                ],
                intelligence_collected=True,
            )
            database.compute_opportunity_confidence("snapshot-1")
            count = database.compute_opportunity_valuation("snapshot-1")

            item = database.opportunities(include_governance=True)["items"][0]

            self.assertEqual(count, 1)
            self.assertEqual(item["valuationStatus"], "valued")
            self.assertEqual(item["monthlyGrossSavings"], 310.0)
            self.assertLess(item["monthlyRiskAdjustedSavings"], 310.0)
            self.assertEqual(item["valuationSource"], "amortized_cost_run_rate")
            self.assertEqual(item["valuationCostSnapshotId"], "snapshot-1")
            self.assertEqual(item["valuationPeriodStart"], "2026-07-01")
            self.assertTrue(item["valuationMethodVersion"])

    def test_database_values_explicit_vm_target_with_retail_price(self):
        with TemporaryDirectory() as temp:
            database = FluxDatabase(Path(temp) / "sku-valuation.duckdb")
            database.init()
            resource_id = (
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "microsoft.compute/virtualmachines/vm-1"
            )
            database.store_snapshot(
                "snapshot-1",
                [
                    {
                        "resourceId": resource_id,
                        "name": "vm-1",
                        "resourceType": "microsoft.compute/virtualmachines",
                        "subscriptionId": "sub",
                        "subscriptionName": "Production",
                        "resourceGroup": "rg",
                        "region": "westus3",
                        "sku": "Standard_D4s_v5",
                        "raw": {
                            "osType": "Windows",
                            "licenseType": "Windows_Server",
                        },
                    }
                ],
                costs=[
                    {
                        "periodStart": "2026-07-01",
                        "periodEnd": "2026-07-10",
                        "costType": "AmortizedCost",
                        "subscriptionId": "sub",
                        "resourceId": resource_id,
                        "amount": 100,
                        "currency": "USD",
                    }
                ],
                cost_scopes=[("sub", "AmortizedCost")],
                advisor=[
                    {
                        "recommendationId": "advisor-1",
                        "resourceId": resource_id,
                        "subscriptionId": "sub",
                        "subscriptionName": "Production",
                        "resourceType": "microsoft.compute/virtualmachines",
                        "category": "Cost",
                        "impact": "High",
                        "problem": "The VM is underutilized.",
                        "solution": "Resize the VM.",
                        "savingsAmount": 100,
                        "annualSavingsAmount": 1200,
                        "savingsCurrency": "USD",
                        "currentSku": "Standard_D4s_v5",
                        "recommendedSku": "Standard_D2s_v5",
                    }
                ],
                advisor_collected=True,
            )
            requests = database.retail_price_requests()
            self.assertEqual(len(requests), 2)
            target_request = next(
                item for item in requests
                if item["targetSku"] == "Standard_D2s_v5"
            )
            self.assertEqual(
                target_request["licenseModel"],
                "azure_hybrid_benefit",
            )
            database.store_retail_prices(
                "retail-1",
                [
                    {
                        **target_request,
                        "status": "matched",
                        "hourlyPrice": 0.1,
                        "monthlyPrice": 73,
                        "hoursPerMonth": 730,
                        "meterId": "meter-1",
                        "meterName": "D2s v5",
                        "productName": "Virtual Machines Dsv5 Series",
                        "skuName": "D2s v5",
                        "unitOfMeasure": "1 Hour",
                        "effectiveStartDate": "2026-01-01T00:00:00Z",
                        "candidateCount": 1,
                        "source": "azure_retail_prices_api",
                        "sourceUrl": "https://prices.azure.com/",
                        "message": "matched",
                        "raw": {},
                    }
                ],
                complete=True,
            )
            database.compute_opportunity_confidence("snapshot-1")
            database.compute_opportunity_valuation("snapshot-1")
            item = database.opportunities()["items"][0]

            self.assertEqual(item["recommendedSku"], "Standard_D2s_v5")
            self.assertEqual(item["currentMonthlyCostRunRate"], 310.0)
            self.assertEqual(item["targetMonthlyRetailCost"], 73.0)
            self.assertEqual(item["monthlyGrossSavings"], 237.0)
            self.assertEqual(
                item["valuationSource"],
                "amortized_cost_minus_retail_target",
            )
            self.assertEqual(item["targetMeterId"], "meter-1")
            self.assertEqual(
                item["priceLicenseModel"],
                "azure_hybrid_benefit",
            )


if __name__ == "__main__":
    unittest.main()
