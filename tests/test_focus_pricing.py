"""Realized-discount math in focus_analytics_report.

Regression for the planning tab showing "Discount realized -640.4K /
-850.9% below list": list_cost is NULL on most FOCUS charges, so comparing
SUM(list) against SUM(effective) over ALL charges subtracts the whole
estate's spend from a fraction of it. The discount must compare only
charges that carry a list price, and disclose how much spend that covers.
"""
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase
from tests.test_geography_report import seed_charge


class RealizedDiscountTests(unittest.TestCase):
    def test_unpriced_charges_cannot_drive_discount_negative(self):
        with TemporaryDirectory() as tmp:
            database = FluxDatabase(Path(tmp) / "flux.duckdb")
            database.init()
            when = datetime(2026, 7, 10, tzinfo=timezone.utc)
            # One priced charge: 40% off list.
            seed_charge(
                database, when=when, region="westus3",
                service_name="Virtual Machines", effective=60.0,
                list_cost=100.0,
            )
            # A much larger unpriced charge -- the old math subtracted this
            # from the 100 of list and reported a -960 "discount".
            seed_charge(
                database, when=when, region="westus3",
                service_name="Azure Backup", effective=1000.0,
            )
            report = database.focus_analytics_report(window_days=30)

        pricing = report["pricing"]
        self.assertAlmostEqual(pricing["discountRealized"], 40.0)
        self.assertAlmostEqual(pricing["discountPercent"], 40.0)
        # 60 of 1060 effective dollars carry a list price.
        self.assertAlmostEqual(
            pricing["pricedSharePercent"], round(60 / 1060 * 100, 1)
        )
        # Per-service: the unpriced service must not claim a discount.
        backup = next(
            row for row in pricing["byService"]
            if row["serviceName"] == "Azure Backup"
        )
        self.assertIsNone(backup["discountPercent"])
        vm = next(
            row for row in pricing["byService"]
            if row["serviceName"] == "Virtual Machines"
        )
        self.assertAlmostEqual(vm["discountPercent"], 40.0)

    def test_no_priced_charges_reports_none_not_negative(self):
        with TemporaryDirectory() as tmp:
            database = FluxDatabase(Path(tmp) / "flux.duckdb")
            database.init()
            seed_charge(
                database,
                when=datetime(2026, 7, 10, tzinfo=timezone.utc),
                region="westus3", service_name="Azure Backup",
                effective=500.0,
            )
            report = database.focus_analytics_report(window_days=30)
        pricing = report["pricing"]
        self.assertEqual(pricing["discountRealized"], 0.0)
        self.assertIsNone(pricing["discountPercent"])
        self.assertEqual(pricing["pricedSharePercent"], 0.0)


if __name__ == "__main__":
    unittest.main()
