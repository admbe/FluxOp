from pathlib import Path
from tempfile import TemporaryDirectory
import csv
import unittest

from api.database import FluxDatabase


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class FinOpsToolkitTests(unittest.TestCase):
    def test_open_data_load_is_versioned_and_idempotent(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            fixtures = {
                "ResourceTypes": [{
                    "ResourceType": "microsoft.compute/virtualmachines",
                    "SingularDisplayName": "Virtual machine",
                    "PluralDisplayName": "Virtual machines",
                    "LowerSingularDisplayName": "virtual machine",
                    "LowerPluralDisplayName": "virtual machines",
                    "IsPreview": "false",
                    "Description": "Compute",
                    "Icon": "https://example.invalid/vm.svg",
                    "Links": "",
                }],
                "Regions": [{
                    "OriginalValue": "East US",
                    "RegionId": "eastus",
                    "RegionName": "East US",
                }],
                "Services": [{
                    "ConsumedService": "Microsoft.Compute",
                    "ResourceType": "microsoft.compute/virtualmachines",
                    "ServiceName": "Virtual Machines",
                    "ServiceCategory": "Compute",
                    "ServiceSubcategory": "Compute",
                    "PublisherName": "Microsoft",
                    "PublisherType": "Cloud Provider",
                    "Environment": "Cloud",
                    "ServiceModel": "IaaS",
                }],
                "PricingUnits": [{
                    "UnitOfMeasure": "1 Hour",
                    "AccountTypes": "MCA, EA",
                    "PricingBlockSize": "1",
                    "DistinctUnits": "Hours",
                }],
                "CommitmentDiscountEligibility": [{
                    "MeterId": "00000000-0000-0000-0000-000000000001",
                    "x_CommitmentDiscountSpendEligibility": "Eligible",
                    "x_CommitmentDiscountUsageEligibility": "Eligible",
                }],
            }
            files = {}
            metadata = {}
            for dataset, rows in fixtures.items():
                path = root / f"{dataset}.csv"
                write_csv(path, rows)
                files[dataset] = path
                metadata[dataset] = {
                    "toolkitVersion": "v14",
                    "upstreamCommit": "commit",
                    "sourceUrl": f"https://example.invalid/{dataset}.csv",
                    "sha256": dataset.lower(),
                    "license": "MIT",
                }

            database = FluxDatabase(root / "toolkit.duckdb")
            database.init()
            first = database.replace_finops_toolkit_open_data(files, metadata)
            second = database.replace_finops_toolkit_open_data(files, metadata)
            status = database.finops_toolkit_status()

            self.assertEqual(first, second)
            self.assertEqual(len(status["datasets"]), 5)
            self.assertTrue(all(item["rowCount"] == 1 for item in status["datasets"]))
            self.assertTrue(all(item["license"] == "MIT" for item in status["datasets"]))
            with database.connect(read_only=True) as connection:
                count = connection.execute(
                    "SELECT count(*) FROM finops_toolkit_resource_types"
                ).fetchone()[0]
                region = connection.execute(
                    "SELECT region_id FROM finops_toolkit_regions"
                ).fetchone()[0]
            self.assertEqual(count, 1)
            self.assertEqual(region, "eastus")


if __name__ == "__main__":
    unittest.main()
