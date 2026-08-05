from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase
from api.expert_explorer import (
    ExpertQueryError,
    run_expert_query,
    validate_expert_sql,
)

VIEWS = {"semantic_daily_cost", "semantic_inventory"}


class ExpertValidationTests(unittest.TestCase):
    def test_accepts_governed_select_with_cte(self):
        sql = (
            "WITH monthly AS (SELECT date_trunc('month', usage_date) AS m, "
            "SUM(amount) AS total FROM semantic_daily_cost "
            "WHERE cost_type = 'ActualCost' GROUP BY 1) "
            "SELECT m, total FROM monthly ORDER BY m"
        )
        self.assertEqual(validate_expert_sql(sql, VIEWS), sql)

    def test_rejects_non_select(self):
        with self.assertRaises(ExpertQueryError):
            validate_expert_sql("DELETE FROM semantic_daily_cost", VIEWS)

    def test_rejects_multiple_statements(self):
        with self.assertRaises(ExpertQueryError):
            validate_expert_sql(
                "SELECT 1; SELECT * FROM semantic_daily_cost", VIEWS
            )

    def test_rejects_writes_hidden_after_select(self):
        for keyword in ("CREATE", "DROP", "ATTACH", "COPY", "SET", "PRAGMA"):
            with self.assertRaises(ExpertQueryError):
                validate_expert_sql(
                    f"SELECT * FROM semantic_daily_cost WHERE {keyword} = 1",
                    VIEWS,
                )

    def test_rejects_file_functions(self):
        with self.assertRaises(ExpertQueryError):
            validate_expert_sql(
                "SELECT * FROM read_csv('/etc/passwd')", VIEWS
            )
        with self.assertRaises(ExpertQueryError):
            validate_expert_sql(
                "SELECT * FROM semantic_daily_cost WHERE amount IN "
                "(SELECT a FROM read_parquet('x'))",
                VIEWS,
            )

    def test_rejects_unknown_relations_and_qualified_names(self):
        with self.assertRaises(ExpertQueryError):
            validate_expert_sql("SELECT * FROM daily_cost_history", VIEWS)
        with self.assertRaises(ExpertQueryError):
            validate_expert_sql(
                "SELECT * FROM information_schema.tables", VIEWS
            )

    def test_keywords_inside_string_literals_are_allowed(self):
        sql = (
            "SELECT * FROM semantic_daily_cost "
            "WHERE service_name = 'CREATE Fabric SET'"
        )
        self.assertEqual(validate_expert_sql(sql, VIEWS), sql)

    def test_comments_cannot_hide_a_second_statement(self):
        with self.assertRaises(ExpertQueryError):
            validate_expert_sql(
                "SELECT 1 /* x */; DROP TABLE semantic_daily_cost", VIEWS
            )


class ExpertExecutionTests(unittest.TestCase):
    def test_row_cap_and_truncation(self):
        with TemporaryDirectory() as temp:
            database = FluxDatabase(Path(temp) / "expert.duckdb")
            database.init()
            result = run_expert_query(
                database,
                "SELECT * FROM range(10) AS t(n)",
                row_limit=5,
            )
            self.assertTrue(result["truncated"])
            self.assertEqual(len(result["rows"]), 5)
            self.assertEqual(result["columns"], ["n"])


if __name__ == "__main__":
    unittest.main()


class ExpertCatalogTextTests(unittest.TestCase):
    """The prompt catalogue must build from the real registry fields.

    It referenced measure.sql, which does not exist on Measure, so every
    Expert Explorer request raised AttributeError and returned HTTP 500
    before any SQL was generated.
    """

    def test_catalog_text_builds_and_names_every_view(self):
        from api.config import settings
        from api.intelligence_assistant import IntelligenceAssistant
        from api.semantic_layer import SEMANTIC_MODELS

        with TemporaryDirectory() as temp:
            database = FluxDatabase(Path(temp) / "catalog.duckdb")
            database.init()
            assistant = IntelligenceAssistant(database, settings)
            text, views = assistant._expert_catalog_text()

        self.assertEqual(
            views, {model.view_name for model in SEMANTIC_MODELS}
        )
        for model in SEMANTIC_MODELS:
            self.assertIn(model.view_name, text)
        # Measure formulas must be the real expressions.
        self.assertIn("total_cost=SUM(amount)", text)
