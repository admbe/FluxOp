import json
import unittest
from unittest.mock import patch

from api.azure import (
    ADVISOR_QUERY,
    ManagedIdentityArgProvider,
    normalize_advisor_recommendations,
)
from api.models import AzureIntegrationUpdate


class FakeToken:
    token = "managed-identity-token"


class FakeCredential:
    def __init__(self):
        self.scope = ""

    def get_token(self, scope):
        self.scope = scope
        return FakeToken()


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ManagedIdentityArgProviderTests(unittest.TestCase):
    def test_normalizes_advisor_recommendation(self):
        recommendations = normalize_advisor_recommendations(
            [
                {
                    "recommendationId": "recommendation-1",
                    "subscriptionId": "SUB-1",
                    "resourceId": "/subscriptions/SUB-1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-1",
                    "resourceType": "Microsoft.Compute/virtualMachines",
                    "category": "Cost",
                    "impact": "High",
                    "problem": "The VM is underutilized.",
                    "solution": "Resize the VM.",
                    "savingsAmount": 25.5,
                    "annualSavingsAmount": 306.0,
                    "savingsCurrency": "USD",
                    "currentSku": "Standard_D8s_v5",
                    "targetSku": "Standard_D4s_v5",
                    "lastUpdated": "2026-07-24T10:00:00Z",
                    "learnMoreLink": "https://learn.microsoft.com/",
                }
            ],
            [{"subscriptionId": "sub-1", "label": "Production"}],
        )

        self.assertEqual(recommendations[0]["subscriptionName"], "Production")
        self.assertEqual(recommendations[0]["category"], "Cost")
        self.assertEqual(recommendations[0]["savingsAmount"], 25.5)
        self.assertEqual(recommendations[0]["annualSavingsAmount"], 306.0)
        self.assertEqual(
            recommendations[0]["recommendedSku"],
            "Standard_D4s_v5",
        )
        self.assertEqual(
            recommendations[0]["resourceType"],
            "microsoft.compute/virtualmachines",
        )

    def test_subscription_advisor_is_deduplicated_and_actionable(self):
        raw = {
            "recommendationId": "reservation-1",
            "subscriptionId": "SUB-1",
            "resourceId": "",
            "resourceType": "Microsoft.Compute/virtualMachines",
            "category": "Cost",
            "impact": "High",
            "problem": "Reservation coverage can reduce cost.",
            "solution": "Review a virtual machine reserved instance.",
            "extendedProperties": {
                "displaySKU": "Standard_D4s_v5",
                "region": "westus3",
                "term": "P3Y",
                "recommendedQuantity": 4,
            },
        }

        duplicate_variant = {
            **raw,
            "recommendationId": "reservation-duplicate-variant",
        }
        recommendations = normalize_advisor_recommendations(
            [raw, raw, duplicate_variant],
            [{"subscriptionId": "sub-1", "label": "Production"}],
        )

        self.assertEqual(len(recommendations), 1)
        self.assertEqual(
            recommendations[0]["resourceId"],
            "/subscriptions/sub-1",
        )
        self.assertEqual(
            recommendations[0]["raw"]["_fluxScopeType"],
            "subscription",
        )
        context = recommendations[0]["raw"]["_fluxActionContext"]
        self.assertIn("Standard_D4s_v5", context)
        self.assertIn("westus3", context)
        self.assertIn("P3Y", context)

    def test_advisor_query_collects_only_active_finops_recommendations(self):
        self.assertIn("category in~ ('Cost', 'Performance')", ADVISOR_QUERY)
        self.assertIn(
            "recommendationStatus in~ ('New', 'InProgress')",
            ADVISOR_QUERY,
        )
        self.assertIn(
            "isempty(properties.tracked) or properties.tracked == false",
            ADVISOR_QUERY,
        )

    def test_integration_payload_accepts_frontend_camel_case_fields(self):
        payload = AzureIntegrationUpdate.model_validate(
            {
                "name": "Azure",
                "tenantId": "00000000-0000-0000-0000-000000000000",
                "enabled": True,
                "authMode": "managed_identity",
                "subscriptions": [
                    {
                        "subscriptionId": "ABCDEF12-3456-7890-ABCD-EF1234567890",
                        "label": "Production",
                    }
                ],
                "lastSyncStatus": "never",
            }
        )

        self.assertEqual(payload.auth_mode, "managed_identity")
        self.assertEqual(
            payload.subscriptions[0].subscription_id,
            "abcdef12-3456-7890-abcd-ef1234567890",
        )

    @patch("api.azure.urlopen")
    def test_managed_identity_queries_and_normalizes_arg(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            {
                "data": [
                    {
                        "id": "/subscriptions/sub-1/resourceGroups/rg/providers/microsoft.compute/disks/disk-1",
                        "name": "disk-1",
                        "resourceType": "Microsoft.Compute/disks",
                        "subscriptionId": "SUB-1",
                        "resourceGroup": "rg",
                        "location": "eastus2",
                        "managedBy": "",
                        "resourceKind": "storage",
                        "tags": {},
                    }
                ]
            }
        )
        credential = FakeCredential()
        provider = ManagedIdentityArgProvider(credential=credential)
        resources = provider.fetch(
            {
                "subscriptions": [
                    {"subscriptionId": "sub-1", "label": "Production"}
                ]
            }
        )

        self.assertEqual(
            credential.scope, "https://management.azure.com/.default"
        )
        self.assertEqual(resources[0]["subscriptionName"], "Production")
        self.assertEqual(resources[0]["kind"], "storage")
        self.assertIsNone(resources[0]["opportunityKind"])
        request = mock_urlopen.call_args.args[0]
        self.assertEqual(
            request.headers["Authorization"], "Bearer managed-identity-token"
        )

    def test_managed_identity_collects_flux_intelligence_rule_packs(self):
        credential = FakeCredential()
        provider = ManagedIdentityArgProvider(credential=credential)
        finding = {
            "ruleId": "public_ip_unattached",
            "resourceId": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Network/publicIPAddresses/ip-1",
            "subscriptionId": "sub-1",
            "resourceGroup": "rg",
            "region": "eastus2",
            "resourceType": "microsoft.network/publicipaddresses",
            "resourceName": "ip-1",
        }
        with patch.object(
            provider,
            "_query",
            side_effect=[[finding]] + [
                [] for _ in provider.intelligence_queries[1:]
            ],
        ) as query:
            findings = provider.fetch_intelligence(
                {
                    "subscriptions": [
                        {"subscriptionId": "sub-1", "label": "Production"}
                    ]
                }
            )

        self.assertEqual(query.call_count, len(provider.intelligence_queries))
        self.assertEqual(findings[0]["source"], "flux_intelligence")
        self.assertEqual(findings[0]["subscriptionName"], "Production")


if __name__ == "__main__":
    unittest.main()
