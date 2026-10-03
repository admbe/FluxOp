import json
import unittest
from unittest.mock import patch

from api.pricing import AzureRetailPriceProvider, price_profile


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class RetailPriceTests(unittest.TestCase):
    def test_azure_hybrid_benefit_uses_base_compute_profile(self):
        self.assertEqual(
            price_profile("Windows", "Windows_Server"),
            ("linux", "azure_hybrid_benefit"),
        )
        self.assertEqual(
            price_profile("Windows", ""),
            ("windows", "license_included"),
        )
        self.assertEqual(
            price_profile("Windows", "Windows_Client"),
            ("linux", "windows_client_entitlement"),
        )

    @patch("api.pricing.urlopen")
    def test_selects_unambiguous_windows_primary_consumption_rate(
        self,
        mock_urlopen,
    ):
        base = {
            "serviceName": "Virtual Machines",
            "armRegionName": "westus3",
            "armSkuName": "Standard_B2as_v2",
            "type": "Consumption",
            "unitOfMeasure": "1 Hour",
            "tierMinimumUnits": 0,
            "isPrimaryMeterRegion": True,
            "currencyCode": "USD",
            "effectiveStartDate": "2026-01-01T00:00:00Z",
        }
        mock_urlopen.return_value = FakeResponse(
            {
                "Items": [
                    {
                        **base,
                        "meterId": "linux",
                        "meterName": "B2as v2",
                        "productName": "Virtual Machines Basv2 Series",
                        "retailPrice": 0.08,
                    },
                    {
                        **base,
                        "meterId": "windows",
                        "meterName": "B2as v2",
                        "productName": (
                            "Virtual Machines Basv2 Series Windows"
                        ),
                        "retailPrice": 0.12,
                        "savingsPlan": [
                            {"term": "1 Year", "retailPrice": 0.09}
                        ],
                    },
                    {
                        **base,
                        "meterId": "spot",
                        "meterName": "B2as v2 Spot",
                        "productName": (
                            "Virtual Machines Basv2 Series Windows"
                        ),
                        "retailPrice": 0.03,
                    },
                    {
                        **base,
                        "type": "Reservation",
                        "reservationTerm": "1 Year",
                        "meterId": "ri-linux",
                        "meterName": "B2as v2",
                        "productName": "Virtual Machines Basv2 Series",
                        "retailPrice": 480.0,
                    },
                ],
                "NextPageLink": None,
            }
        )
        result = AzureRetailPriceProvider(
            request_delay_ms=0,
        ).fetch_one(
            {
                "region": "westus3",
                "targetSku": "Standard_B2as_v2",
                "operatingSystem": "windows",
                "licenseModel": "license_included",
                "priceProfile": "windows",
                "currency": "USD",
            }
        )

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["meterId"], "windows")
        self.assertEqual(result["hourlyPrice"], 0.12)
        self.assertEqual(result["monthlyPrice"], 87.6)
        self.assertEqual(result["monthlyComputePrice"], 58.4)
        self.assertEqual(result["monthlyLicensePrice"], 29.2)
        self.assertEqual(result["ri1YearUpfront"], 480.0)
        self.assertEqual(result["ri1YearMonthly"], 69.2)
        self.assertEqual(result["sp1YearMonthly"], 65.7)

    @patch("api.pricing.urlopen")
    def test_distinct_current_rates_are_ambiguous(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            {
                "Items": [
                    {
                        "serviceName": "Virtual Machines",
                        "armRegionName": "westus3",
                        "armSkuName": "Standard_D2s_v5",
                        "type": "Consumption",
                        "unitOfMeasure": "1 Hour",
                        "tierMinimumUnits": 0,
                        "isPrimaryMeterRegion": True,
                        "currencyCode": "USD",
                        "effectiveStartDate": "2026-01-01T00:00:00Z",
                        "meterId": "one",
                        "meterName": "D2s v5",
                        "productName": "Virtual Machines Dsv5 Series",
                        "retailPrice": 0.1,
                    },
                    {
                        "serviceName": "Virtual Machines",
                        "armRegionName": "westus3",
                        "armSkuName": "Standard_D2s_v5",
                        "type": "Consumption",
                        "unitOfMeasure": "1 Hour",
                        "tierMinimumUnits": 0,
                        "isPrimaryMeterRegion": True,
                        "currencyCode": "USD",
                        "effectiveStartDate": "2026-01-01T00:00:00Z",
                        "meterId": "two",
                        "meterName": "D2s v5",
                        "productName": "Virtual Machines Dsv5 Series",
                        "retailPrice": 0.2,
                    },
                ],
                "NextPageLink": None,
            }
        )
        result = AzureRetailPriceProvider(
            request_delay_ms=0,
        ).fetch_one(
            {
                "region": "westus3",
                "targetSku": "Standard_D2s_v5",
                "operatingSystem": "linux",
                "licenseModel": "linux",
                "priceProfile": "linux",
                "currency": "USD",
            }
        )

        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result.get("monthlyPrice"))

    @patch("api.pricing.urlopen")
    def test_older_series_price_in_newer_region_is_not_discarded(
        self,
        mock_urlopen,
    ):
        """Azure marks legitimate v3-era prices isPrimaryMeterRegion=False.

        Standard_D2s_v3 in westus3 returns exactly one Linux consumption
        meter, flagged False because the meter's billing home is elsewhere.
        Requiring True discarded the only valid rate and left every v2/v3-era
        SKU unpriced, silently under-valuing right-sizing targets.
        """
        mock_urlopen.return_value = FakeResponse(
            {
                "Items": [
                    {
                        "serviceName": "Virtual Machines",
                        "armRegionName": "westus3",
                        "armSkuName": "Standard_D2s_v3",
                        "type": "Consumption",
                        "unitOfMeasure": "1 Hour",
                        "tierMinimumUnits": 0,
                        "isPrimaryMeterRegion": False,
                        "currencyCode": "USD",
                        "effectiveStartDate": "2022-08-01T00:00:00Z",
                        "meterId": "linux-d2sv3",
                        "meterName": "D2s v3",
                        "productName": "Virtual Machines DSv3 Series",
                        "retailPrice": 0.096,
                        "savingsPlan": [
                            {"term": "1 Year", "retailPrice": 0.06624},
                        ],
                    },
                ],
                "NextPageLink": None,
            }
        )
        result = AzureRetailPriceProvider(request_delay_ms=0).fetch_one(
            {
                "region": "westus3",
                "targetSku": "Standard_D2s_v3",
                "operatingSystem": "linux",
                "licenseModel": "linux",
                "priceProfile": "linux",
                "currency": "USD",
            }
        )

        self.assertEqual(result["status"], "matched")
        self.assertAlmostEqual(result["monthlyPrice"], 70.08, places=2)
        # Azure publishes no reservation meter for this series; the savings
        # plan rate is the available 1-year commitment price.
        self.assertIsNone(result["ri1YearMonthly"])
        self.assertAlmostEqual(result["sp1YearMonthly"], 48.36, places=2)
