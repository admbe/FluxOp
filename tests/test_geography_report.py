"""Geography cost report: region buckets, domains, ESR from FOCUS charges."""
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from api.database import FluxDatabase


def make_database(tmp: str) -> FluxDatabase:
    database = FluxDatabase(Path(tmp) / "flux.duckdb")
    database.init()
    return database


def seed_manifest(database: FluxDatabase, manifest_id: str = "m1") -> None:
    """Register the manifest these fixture charges belong to.

    Reports read focus_cost_current, which INNER JOINs focus_manifests_current
    (focus_export_manifests WHERE status = 'imported'). A fixture that inserts
    charges with a literal manifest_id and no manifest row is invisible to
    every report -- which is the correct production behaviour for an
    unregistered charge, so the fixture has to register one.
    """
    with database.connect() as db:
        db.execute(
            """
            INSERT OR IGNORE INTO focus_export_manifests (
                manifest_id, import_run_id, manifest_path, export_name,
                export_run_id, subscription_id, subscription_name,
                period_start, period_end, imported_at, status, data_version
            ) VALUES (?, 'run-1', ?, 'focus-test', 'run-1', 'sub-1',
                'prod-sub', DATE '2000-01-01', DATE '2999-12-31',
                now(), 'imported', '1.0')
            """,
            [manifest_id, f"focus/{manifest_id}/manifest.json"],
        )
        db.commit()


def seed_charge(
    database: FluxDatabase,
    *,
    when: datetime,
    region: str,
    service_name: str,
    service_category: str = "",
    meter_subcategory: str = "",
    effective: float = 100.0,
    list_cost: float | None = None,
    contracted: float | None = None,
) -> None:
    seed_manifest(database)
    with database.connect() as db:
        db.execute(
            """
            INSERT INTO focus_cost_charges (
                charge_id, manifest_id, charge_period_start, billed_cost,
                effective_cost, contracted_cost, list_cost,
                billing_currency, charge_category, charge_class,
                charge_frequency, charge_description, pricing_category,
                consumed_unit, pricing_unit, commitment_discount_id,
                commitment_discount_name, commitment_discount_category,
                commitment_discount_type, service_category, service_name,
                resource_id, resource_name, resource_type, resource_group,
                subscription_id, subscription_name, provider_name,
                publisher_name, region_name, sku_id, sku_price_id,
                meter_id, meter_name, meter_category, meter_subcategory,
                tags_json, raw_json
            ) VALUES (?, 'm1', ?, ?, ?, ?, ?, 'USD', 'Usage', 'Standard',
                'Usage', 'test', 'Standard', 'Hours', 'Hours', '', '', '',
                '', ?, ?, 'rid', 'res', 'type', 'rg', 'sub-1', 'prod-sub',
                'Microsoft', 'Microsoft', ?, 'sku', 'skuprice', 'meter',
                'meter', 'cat', ?, '{}', '{}')
            """,
            [
                str(uuid4()), when, effective, effective, contracted,
                list_cost, service_category, service_name, region,
                meter_subcategory,
            ],
        )
        db.commit()


class GeographyReportTests(unittest.TestCase):
    def test_not_connected_without_charges(self):
        with TemporaryDirectory() as tmp:
            database = make_database(tmp)
            self.assertEqual(
                database.geography_cost_report()["status"], "not_connected"
            )

    def test_region_bucketing_and_esr(self):
        with TemporaryDirectory() as tmp:
            database = make_database(tmp)
            june = datetime(2026, 6, 10, tzinfo=timezone.utc)
            july = datetime(2026, 7, 10, tzinfo=timezone.utc)
            month_end = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
            # westus3 VM with a 40% discount off list
            seed_charge(
                database, when=july, region="westus3",
                service_name="Virtual Machines", effective=60.0,
                list_cost=100.0, contracted=70.0,
            )
            # uksouth backup with no list price -> excluded from ESR
            seed_charge(
                database, when=july, region="uksouth",
                service_name="Azure Backup", effective=50.0,
            )
            # global/blank region rolls into shared
            seed_charge(
                database, when=july, region="",
                service_name="Azure DNS", service_category="Networking",
                effective=10.0,
            )
            # prior month for movers
            seed_charge(
                database, when=june, region="westus3",
                service_name="Virtual Machines", effective=40.0,
                list_cost=80.0,
            )
            # anchor charge near month-end so July counts as complete
            seed_charge(
                database, when=month_end, region="westus3",
                service_name="Virtual Machines", effective=1.0,
                list_cost=1.0,
            )
            report = database.geography_cost_report()

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["latestMonth"], "2026-07")
        self.assertTrue(report["latestMonthComplete"])
        # bucketing
        self.assertAlmostEqual(report["share"]["wus3"], 61.0)
        self.assertAlmostEqual(report["share"]["uks"], 50.0)
        self.assertAlmostEqual(report["share"]["shared"], 10.0)
        # ESR only over list-priced charges: wus3 = 1 - 61/101
        wus3 = report["pricingByGeo"]["wus3"]
        self.assertAlmostEqual(wus3["esr"], round((1 - 101 / 181) * 100, 1))
        # uksouth has no list price at all -> esr is None, pricedShare 0
        uks = report["pricingByGeo"]["uks"]
        self.assertIsNone(uks["esr"])
        self.assertEqual(uks["pricedShare"], 0.0)
        # movers: wus3 rose 40 -> 61
        wus3_mover = next(
            item for item in report["movers"] if item["geo"] == "wus3"
        )
        self.assertAlmostEqual(wus3_mover["delta"], 21.0)
        # domain mapping
        self.assertAlmostEqual(
            report["matrix"]["uks"]["backup"], 50.0
        )
        self.assertAlmostEqual(
            report["matrix"]["shared"]["network"], 10.0
        )

    def test_partial_latest_month_moves_share_back(self):
        with TemporaryDirectory() as tmp:
            database = make_database(tmp)
            seed_charge(
                database,
                when=datetime(2026, 6, 20, tzinfo=timezone.utc),
                region="westus3", service_name="Virtual Machines",
                effective=100.0,
            )
            seed_charge(
                database,
                when=datetime(2026, 7, 3, tzinfo=timezone.utc),
                region="westus3", service_name="Virtual Machines",
                effective=5.0,
            )
            report = database.geography_cost_report()
        self.assertFalse(report["latestMonthComplete"])
        # share month falls back to the last complete month
        self.assertEqual(report["shareMonth"], "2026-06")
        self.assertAlmostEqual(report["share"]["wus3"], 100.0)


if __name__ == "__main__":
    unittest.main()
