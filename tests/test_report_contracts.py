from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import duckdb

from api.database import FluxDatabase
from api.evidence import evidence_markdown, opportunity_evidence
from api.report_catalog import validate_report_request
from scripts.check_finops_toolkit_drift import classify_artifact


class ReportContractTests(unittest.TestCase):
    def test_catalog_rejects_arbitrary_sql_and_unknown_fields(self):
        with self.assertRaisesRegex(ValueError, "Arbitrary SQL"):
            validate_report_request(
                {"reportId": "cost-summary", "sql": "select * from secrets"}
            )
        with self.assertRaisesRegex(ValueError, "undeclared"):
            validate_report_request(
                {
                    "reportId": "cost-summary",
                    "measures": ["password"],
                    "dimensions": [],
                    "filters": {},
                }
            )

    def test_catalog_approves_declared_contract(self):
        result = validate_report_request(
            {
                "reportId": "cost-summary",
                "measures": ["totalCost", "forecast"],
                "dimensions": ["subscription"],
                "filters": {"costType": "AmortizedCost"},
            }
        )
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["endpoint"], "/api/reports/cost")

    def test_catalog_approves_focus_charge_contract(self):
        result = validate_report_request(
            {
                "reportId": "focus-cost-investigation",
                "measures": ["billedCost", "effectiveCost"],
                "dimensions": ["chargeCategory", "commitmentDiscountType"],
                "filters": {"subscriptionId": "sub-1"},
            }
        )
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["endpoint"], "/api/reports/focus-cost")

    def test_toolkit_drift_classifies_report_artifacts(self):
        self.assertEqual(
            classify_artifact("src/power-bi/CostSummary/Measures.dax"),
            "measure",
        )
        self.assertEqual(
            classify_artifact("src/recommendations/rules/storage.json"),
            "rule",
        )
        self.assertEqual(
            classify_artifact("src/power-bi/CostSummary/report.json"),
            "report-feature",
        )

    def test_rill_models_compile_against_governed_duckdb_views(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as folder:
            database_path = Path(folder) / "rill-contract.duckdb"
            database = FluxDatabase(database_path)
            database.init()
            models = sorted((root / "rill" / "models").glob("*.yaml"))
            with duckdb.connect() as connection:
                connection.execute(
                    f"ATTACH '{database_path.as_posix()}' AS flux (READ_ONLY)"
                )
                for model in models:
                    lines = model.read_text(encoding="utf-8").splitlines()
                    start = lines.index("sql: |") + 1
                    sql = "\n".join(
                        line[2:] for line in lines[start:] if line.startswith("  ")
                    )
                    connection.execute(f"SELECT * FROM ({sql}) LIMIT 0")

    def test_rill_cost_measure_matches_native_cost_report(self):
        with TemporaryDirectory() as folder:
            database = FluxDatabase(Path(folder) / "parity.duckdb")
            database.init()
            database.store_daily_cost_scope(
                "history",
                "sub-1",
                "AmortizedCost",
                [
                    {
                        "usageDate": "2026-07-01",
                        "costType": "AmortizedCost",
                        "subscriptionId": "sub-1",
                        "resourceId": "/resource/one",
                        "serviceName": "Compute",
                        "amount": 42,
                        "currency": "USD",
                        "source": "test",
                    }
                ],
            )
            native = database.cost_report(
                cost_type="AmortizedCost",
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 1),
            )
            with database.connect(read_only=True) as connection:
                semantic = connection.execute(
                    """
                    SELECT sum(amount) FROM daily_cost_history
                    WHERE cost_type = 'AmortizedCost' AND currency = 'USD'
                    """
                ).fetchone()[0]
            self.assertEqual(native["summary"]["totalCost"], semantic)

    def test_evidence_is_a_deterministic_change_request(self):
        pack = opportunity_evidence(
            {
                "id": "one",
                "title": "Resize VM",
                "resourceId": "/resource/one",
                "resourceName": "one",
                "source": "azure_advisor",
                "confidence": "High",
            },
            None,
            {
                "reason": (
                    "CPU p95 is 12.0%, below the governed 30.0% "
                    "resize-review limit."
                )
            },
        )
        document = evidence_markdown(pack)
        self.assertIn("change-request draft", document)
        self.assertIn("Decision rationale", document)
        self.assertIn("CPU p95 is 12.0%", document)
        self.assertIn("Validation checklist", document)
        self.assertIn("Rollback", document)


if __name__ == "__main__":
    unittest.main()
