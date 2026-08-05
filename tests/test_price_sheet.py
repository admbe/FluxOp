from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase
from api.semantic_layer import SemanticQuery

MCA_STYLE_CSV = """meterId,meterName,serviceFamily,product,unitOfMeasure,priceType,unitPrice,basePrice,marketPrice,currency
m-1,D2as v5,Compute,Virtual Machines Dasv5,1 Hour,Consumption,0.081,0.096,0.096,USD
m-2,E2as v7,Compute,Virtual Machines Easv7,1 Hour,Consumption,0.099,0.110,0.110,USD
m-3,Hot LRS Data,Storage,Blob Storage,1 GB/Month,Consumption,0.018,,0.018,USD
"""

EA_STYLE_CSV = """meterID,meterName,unitPrice,marketPrice,currencyCode,partNumber
m-9,Legacy Meter,0.5,1.0,USD,PN-1
"""


class PriceSheetTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "p.duckdb")
        self.database.init()

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, name: str, content: str) -> Path:
        path = Path(self.temp.name) / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_import_normalizes_mca_columns(self):
        rows = self.database.store_price_sheet(
            [self._write("mca.csv", MCA_STYLE_CSV)]
        )
        self.assertEqual(rows, 3)
        result = self.database.run_semantic_query(
            SemanticQuery(
                model="price_sheet",
                measures=(
                    "meter_count",
                    "discounted_meters",
                    "average_discount_percent",
                ),
            )
        )
        meter_count, discounted, discount = result["rows"][0]
        self.assertEqual(meter_count, 3)
        self.assertEqual(discounted, 2, "the storage meter has no discount")
        self.assertAlmostEqual(discount, (15.625 + 10.0 + 0.0) / 3, places=2)

    def test_import_normalizes_ea_column_variants(self):
        rows = self.database.store_price_sheet(
            [self._write("ea.csv", EA_STYLE_CSV)]
        )
        self.assertEqual(rows, 1)
        with self.database.connect(read_only=True) as db:
            row = db.execute(
                "SELECT meter_id, sku_id, currency, unit_price, base_price "
                "FROM price_sheet_current"
            ).fetchone()
        self.assertEqual(row[0], "m-9")
        self.assertEqual(row[1], "PN-1")
        self.assertEqual(row[2], "USD")
        self.assertEqual(row[3], 0.5)
        self.assertIsNone(row[4], "EA sheets have no basePrice column")

    def test_reimport_replaces_wholesale(self):
        self.database.store_price_sheet(
            [self._write("first.csv", MCA_STYLE_CSV)]
        )
        self.database.store_price_sheet(
            [self._write("second.csv", EA_STYLE_CSV)]
        )
        with self.database.connect(read_only=True) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM price_sheet_current"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_model_available_before_first_import(self):
        catalog = self.database.semantic_catalog()
        price_sheet = next(
            model for model in catalog["models"]
            if model["name"] == "price_sheet"
        )
        self.assertTrue(price_sheet["available"])
        result = self.database.run_semantic_query(
            SemanticQuery(model="price_sheet", measures=("meter_count",))
        )
        self.assertEqual(result["rows"][0][0], 0)


if __name__ == "__main__":
    unittest.main()
