from __future__ import annotations

import csv
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase
from api.focus import focus_error_message, import_manifests, local_manifests


FIELDS = [
    "BilledCost", "BillingCurrency", "BillingPeriodEnd", "BillingPeriodStart",
    "ChargeCategory", "ChargeClass", "ChargeDescription", "ChargeFrequency",
    "ChargePeriodEnd", "ChargePeriodStart", "CommitmentDiscountCategory",
    "CommitmentDiscountId", "CommitmentDiscountName", "CommitmentDiscountType",
    "ConsumedQuantity", "ConsumedUnit", "ContractedCost",
    "ContractedUnitPrice", "EffectiveCost", "ListCost", "ListUnitPrice",
    "PricingCategory", "PricingQuantity", "PricingUnit", "ProviderName",
    "PublisherName", "RegionName", "ResourceId", "ResourceName", "ResourceType",
    "ServiceCategory", "ServiceName", "SkuId", "SkuPriceId", "SubAccountId",
    "SubAccountName", "Tags", "x_ResourceGroupName", "x_SkuMeterId",
    "x_SkuMeterName", "x_SkuMeterCategory", "x_SkuMeterSubcategory",
]


class FocusImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = FluxDatabase(self.root / "test.duckdb")
        self.database.init()
        run = self.root / "dev-sub" / "daily" / "202607" / "run-1"
        run.mkdir(parents=True)
        csv_path = run / "part_0.csv"
        rows = [
            {
                "BilledCost": "10", "EffectiveCost": "8",
                "BillingCurrency": "USD",
                "ChargePeriodStart": "2026-07-02T00:00Z",
                "ChargePeriodEnd": "2026-07-03T00:00Z",
                "BillingPeriodStart": "2026-07-01T00:00Z",
                "BillingPeriodEnd": "2026-08-01T00:00Z",
                "ChargeCategory": "Usage", "ServiceName": "Virtual Machines",
                "ResourceId": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
                "ResourceName": "vm1", "SubAccountId": "sub-1",
                "SubAccountName": "dev-sub", "Tags": '{"owner":"cloud"}',
            },
            {
                "BilledCost": "5", "EffectiveCost": "0",
                "BillingCurrency": "USD",
                "ChargePeriodStart": "2026-07-02T00:00Z",
                "ChargePeriodEnd": "2026-07-03T00:00Z",
                "BillingPeriodStart": "2026-07-01T00:00Z",
                "BillingPeriodEnd": "2026-08-01T00:00Z",
                "ChargeCategory": "Purchase", "ServiceName": "Reservations",
                "ResourceId": "", "ResourceName": "", "SubAccountId": "sub-1",
                "SubAccountName": "dev-sub", "Tags": "{}",
            },
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        manifest = {
            "manifestVersion": "2024-04-01",
            "byteCount": csv_path.stat().st_size,
            "dataRowCount": 2,
            "exportConfig": {
                "exportName": "focus-daily-dev-sub",
                "resourceId": "/subscriptions/sub-1/providers/Microsoft.CostManagement/exports/focus-daily-dev-sub",
                "dataVersion": "1.0",
                "type": "FocusCost",
            },
            "runInfo": {
                "runId": "run-1",
                "startDate": "2026-07-01T00:00:00",
                "endDate": "2026-07-31T00:00:00Z",
                "submittedTime": "2026-07-26T08:43:03.3381033Z",
            },
            "blobs": [
                {
                    "blobName": "focus/dev-sub/daily/202607/run-1/part_0.csv",
                    "dataRowCount": 2,
                    "byteCount": csv_path.stat().st_size,
                }
            ],
        }
        (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_import_is_idempotent_and_promotes_actual_and_amortized_cost(self):
        manifests = local_manifests(self.root)
        self.database.start_focus_import("focus-1")
        first = import_manifests(
            self.database, "focus-1", manifests, maximum_manifests=10
        )
        self.assertEqual(first, {"imported": 1, "skipped": 0, "charges": 2})
        second = import_manifests(
            self.database, "focus-2", manifests, maximum_manifests=10
        )
        self.assertEqual(second, {"imported": 0, "skipped": 1, "charges": 0})
        with self.database.connect(read_only=True) as db:
            totals = db.execute(
                """
                SELECT cost_type, sum(amount)
                FROM daily_cost_history GROUP BY cost_type ORDER BY cost_type
                """
            ).fetchall()
            charges = db.execute(
                "SELECT count(*) FROM focus_cost_charges"
            ).fetchone()[0]
        self.assertEqual(charges, 2)
        self.assertEqual(totals, [("ActualCost", 15.0), ("AmortizedCost", 8.0)])

    def test_query_collector_cannot_overwrite_focus_dates(self):
        manifests = local_manifests(self.root)
        import_manifests(
            self.database, "focus-1", manifests, maximum_manifests=10
        )
        stored = self.database.store_daily_cost_scope(
            "query-1",
            "sub-1",
            "ActualCost",
            [
                {
                    "usageDate": date(2026, 7, 2),
                    "resourceId": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
                    "serviceName": "Virtual Machines",
                    "amount": 999,
                    "currency": "USD",
                }
            ],
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
        )
        self.assertEqual(stored, 0)
        with self.database.connect(read_only=True) as db:
            total = db.execute(
                """
                SELECT sum(amount) FROM daily_cost_history
                WHERE cost_type = 'ActualCost'
                """
            ).fetchone()[0]
        self.assertEqual(total, 15.0)

    def test_query_collector_filters_string_usage_dates_without_error(self):
        """The Query API provider yields ISO-string usageDates.

        Production incident 2026-07-30..08-01: comparing those strings to the
        DATE-typed FOCUS manifest periods raised TypeError, failing every
        Query-API cost sync for exactly the subscriptions that had imported
        FOCUS manifests. The prior test missed it by passing date objects.
        """
        import_manifests(
            self.database,
            "focus-1",
            local_manifests(self.root),
            maximum_manifests=10,
        )
        stored = self.database.store_daily_cost_scope(
            "query-2",
            "sub-1",
            "ActualCost",
            [
                {
                    "usageDate": "2026-07-02",
                    "resourceId": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
                    "serviceName": "Virtual Machines",
                    "amount": 999,
                    "currency": "USD",
                },
                {
                    "usageDate": "2026-06-15",
                    "resourceId": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
                    "serviceName": "Virtual Machines",
                    "amount": 7,
                    "currency": "USD",
                },
            ],
            start_date=date(2026, 6, 1),
            end_date=date(2026, 7, 31),
        )
        self.assertEqual(stored, 1)
        with self.database.connect(read_only=True) as db:
            kept = db.execute(
                """
                SELECT usage_date, amount FROM daily_cost_history
                WHERE cost_type = 'ActualCost'
                  AND source <> 'azure_focus_export'
                """
            ).fetchall()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0][0], date(2026, 6, 15))
        self.assertEqual(kept[0][1], 7)

    def test_governed_focus_report_exposes_charge_drivers_and_coverage(self):
        self.database.save_integration({
            "name": "Azure",
            "tenantId": "",
            "enabled": True,
            "authMode": "managed_identity",
            "subscriptions": [
                {"subscriptionId": "sub-1", "label": "dev-sub"},
                {"subscriptionId": "sub-2", "label": "prod-sub"},
            ],
        })
        import_manifests(
            self.database,
            "focus-1",
            local_manifests(self.root),
            maximum_manifests=10,
        )
        report = self.database.focus_cost_report(
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
        )
        self.assertEqual(report["summary"]["billedCost"], 15.0)
        self.assertEqual(report["summary"]["effectiveCost"], 8.0)
        self.assertEqual(report["summary"]["purchaseBilledCost"], 5.0)
        self.assertEqual(report["summary"]["resourceCount"], 1)
        self.assertEqual(
            [item["name"] for item in report["byChargeCategory"]],
            ["Usage", "Purchase"],
        )
        self.assertEqual(report["resources"][0]["resourceName"], "vm1")
        self.assertFalse(report["coverage"]["complete"])
        self.assertEqual(report["coverage"]["availableScopes"], 1)
        self.assertEqual(
            report["coverage"]["missingScopes"][0]["name"],
            "prod-sub",
        )

    def test_storage_permission_failure_names_the_required_data_role(self):
        message = focus_error_message(
            RuntimeError(
                "AuthorizationPermissionMismatch: This request is not "
                "authorized to perform this operation."
            )
        )

        self.assertIn("Storage Blob Data Reader", message)
        self.assertIn("managed identity", message)


if __name__ == "__main__":
    unittest.main()


class FocusSubscriptionNormalizationTests(unittest.TestCase):
    """Azure FOCUS exports carry SubAccountId as the full ARM path.

    Every other Flux path keys on the bare GUID, so an unnormalized import
    splits one subscription into two scopes and prevents FOCUS cost from
    joining inventory. The original fixtures used a bare SubAccountId, which
    is why this went unnoticed.
    """

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = FluxDatabase(self.root / "focus-norm.duckdb")
        self.database.init()

    def tearDown(self):
        self.temp.cleanup()

    def _write_export(self, sub_account_id: str) -> None:
        run = self.root / "prod" / "daily" / "202607" / "run-1"
        run.mkdir(parents=True, exist_ok=True)
        csv_path = run / "part_0.csv"
        rows = [
            {
                "BilledCost": "40", "EffectiveCost": "32",
                "BillingCurrency": "USD",
                "ChargePeriodStart": "2026-07-02T00:00Z",
                "ChargePeriodEnd": "2026-07-03T00:00Z",
                "BillingPeriodStart": "2026-07-01T00:00Z",
                "BillingPeriodEnd": "2026-08-01T00:00Z",
                "ChargeCategory": "Usage", "ServiceName": "Virtual Machines",
                "ResourceId": "/subscriptions/sub-9/resourcegroups/rg/providers/microsoft.compute/virtualmachines/vm9",
                "ResourceName": "vm9", "SubAccountId": sub_account_id,
                "SubAccountName": "prod", "Tags": "{}",
            },
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        manifest = {
            "manifestVersion": "2024-04-01",
            "byteCount": csv_path.stat().st_size,
            "dataRowCount": 1,
            "exportConfig": {
                "exportName": "focus-daily-prod",
                "resourceId": "/subscriptions/sub-9/providers/Microsoft.CostManagement/exports/focus-daily-prod",
                "dataVersion": "1.0",
                "type": "FocusCost",
            },
            "runInfo": {
                "runId": "run-1",
                "startDate": "2026-07-01T00:00:00",
                "endDate": "2026-07-31T00:00:00Z",
                "submittedTime": "2026-07-26T08:43:03.3381033Z",
            },
            "blobs": [{
                "blobName": "focus/prod/daily/202607/run-1/part_0.csv",
                "dataRowCount": 1,
                "byteCount": csv_path.stat().st_size,
            }],
        }
        (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def _subscription_ids(self, table: str) -> set[str]:
        with self.database.connect(read_only=True) as db:
            return {
                str(row[0])
                for row in db.execute(
                    f"SELECT DISTINCT subscription_id FROM {table}"
                ).fetchall()
            }

    def test_arm_path_sub_account_is_normalized_on_import(self):
        self._write_export("/subscriptions/sub-9")
        self.database.start_focus_import("focus-norm")
        import_manifests(
            self.database, "focus-norm", local_manifests(self.root),
            maximum_manifests=10,
        )

        self.assertEqual(self._subscription_ids("focus_cost_charges"), {"sub-9"})
        self.assertEqual(self._subscription_ids("daily_cost_history"), {"sub-9"})

    def test_bare_sub_account_still_imports_unchanged(self):
        self._write_export("sub-9")
        self.database.start_focus_import("focus-norm")
        import_manifests(
            self.database, "focus-norm", local_manifests(self.root),
            maximum_manifests=10,
        )

        self.assertEqual(self._subscription_ids("focus_cost_charges"), {"sub-9"})
        self.assertEqual(self._subscription_ids("daily_cost_history"), {"sub-9"})

    def test_focus_cost_joins_inventory_on_the_same_key(self):
        self._write_export("/subscriptions/sub-9")
        self.database.start_focus_import("focus-norm")
        import_manifests(
            self.database, "focus-norm", local_manifests(self.root),
            maximum_manifests=10,
        )
        self.database.store_snapshot(
            "inventory-1",
            [{
                "resourceId": "/subscriptions/sub-9/resourcegroups/rg/providers/microsoft.compute/virtualmachines/vm9",
                "name": "vm9",
                "resourceType": "microsoft.compute/virtualmachines",
                "subscriptionId": "sub-9", "subscriptionName": "prod",
                "resourceGroup": "rg", "region": "westus3", "tags": {},
            }],
        )
        with self.database.connect(read_only=True) as db:
            joined = db.execute(
                """
                SELECT count(*)
                FROM daily_cost_history AS cost
                JOIN resources_current AS resource
                  ON resource.subscription_id = cost.subscription_id
                WHERE cost.source = 'azure_focus_export'
                """
            ).fetchone()[0]
        self.assertGreater(joined, 0, "FOCUS cost must join inventory by subscription")

    def test_migration_backfills_legacy_arm_path_rows(self):
        # Simulate a database written before normalization.
        with self.database.connect() as db:
            db.execute(
                """
                INSERT INTO daily_cost_history VALUES (
                    'legacy-run', now(), DATE '2026-07-02', 'AmortizedCost',
                    '/subscriptions/sub-legacy', '/r/vm-legacy',
                    'Virtual Machines', 12.5, 'USD', 'azure_focus_export'
                )
                """
            )
            db.execute(
                "DELETE FROM schema_migrations WHERE version = ?",
                ["20260729_normalize_focus_subscription_id"],
            )

        self.database.init()

        self.assertIn("sub-legacy", self._subscription_ids("daily_cost_history"))
        self.assertNotIn(
            "/subscriptions/sub-legacy",
            self._subscription_ids("daily_cost_history"),
        )

    def test_migration_prefers_focus_row_when_normalization_collides(self):
        with self.database.connect() as db:
            for subscription_id, amount, source in (
                ("/subscriptions/sub-c", 99.0, "azure_focus_export"),
                ("sub-c", 11.0, "azure_cost_management_query"),
            ):
                db.execute(
                    """
                    INSERT INTO daily_cost_history VALUES (
                        'run-c', now(), DATE '2026-07-03', 'AmortizedCost',
                        ?, '/r/vm-c', 'Virtual Machines', ?, 'USD', ?
                    )
                    """,
                    [subscription_id, amount, source],
                )
            db.execute(
                "DELETE FROM schema_migrations WHERE version = ?",
                ["20260729_normalize_focus_subscription_id"],
            )

        self.database.init()

        with self.database.connect(read_only=True) as db:
            rows = db.execute(
                """
                SELECT source, amount FROM daily_cost_history
                WHERE subscription_id = 'sub-c'
                """
            ).fetchall()
        self.assertEqual(
            rows, [("azure_focus_export", 99.0)],
            "FOCUS is authoritative for periods it covers",
        )
