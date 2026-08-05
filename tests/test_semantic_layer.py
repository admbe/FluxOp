from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase
from api.semantic_layer import (
    SEMANTIC_MODELS,
    SemanticQuery,
    SemanticQueryError,
    build_semantic_query,
    create_semantic_views,
    find_model,
    semantic_catalog,
)


class SemanticRegistryTests(unittest.TestCase):
    def test_registry_names_are_unique_and_well_formed(self):
        model_names = [model.name for model in SEMANTIC_MODELS]
        self.assertEqual(len(model_names), len(set(model_names)))
        for model in SEMANTIC_MODELS:
            self.assertTrue(model.description)
            self.assertTrue(model.grain)
            members = [d.name for d in model.dimensions] + [
                m.name for m in model.measures
            ]
            self.assertEqual(
                len(members), len(set(members)),
                f"duplicate member name in {model.name}",
            )
            for measure in model.measures:
                self.assertIn(measure.format, {"number", "currency", "percent"})
                self.assertIn(measure.higher_is, {"good", "bad", "neutral"})

    def test_catalog_shape(self):
        catalog = semantic_catalog()
        self.assertIn("contract", catalog)
        names = {model["name"] for model in catalog["models"]}
        self.assertIn("daily_cost", names)
        self.assertIn("focus_cost", names)


class SemanticViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "semantic.duckdb")
        self.database.init()
        self.database.seed_demo()

    def tearDown(self):
        self.temp.cleanup()

    def test_every_model_view_exists_and_selects(self):
        # init() runs create_semantic_views through the materialized-table
        # rebuild; every registry model must be queryable end to end.
        with self.database.connect(read_only=True) as db:
            for model in SEMANTIC_MODELS:
                row = db.execute(
                    f"SELECT COUNT(*) FROM {model.view_name}"
                ).fetchone()
                self.assertIsNotNone(row, model.view_name)
                for measure in model.measures:
                    db.execute(
                        f"SELECT {measure.expression} FROM {model.view_name}"
                    ).fetchone()
                for dimension in model.dimensions:
                    db.execute(
                        f"SELECT {dimension.column} FROM {model.view_name}"
                        " LIMIT 1"
                    )

    def test_create_semantic_views_is_idempotent(self):
        with self.database.connect() as db:
            first = create_semantic_views(db)
            second = create_semantic_views(db)
        self.assertEqual(first, second)
        self.assertEqual(len(first), len(SEMANTIC_MODELS))

    def test_query_totals_match_hand_sql(self):
        with self.database.connect() as db:
            db.execute(
                """
                INSERT INTO daily_cost_history (
                    snapshot_id, usage_date, cost_type, subscription_id,
                    resource_id, service_name, amount, currency, source,
                    observed_at
                ) VALUES
                ('s1', DATE '2026-07-01', 'ActualCost', 'sub-1', 'r-1',
                 'virtualmachines', 40.0, 'USD', 'cost-history', now()),
                ('s1', DATE '2026-07-02', 'ActualCost', 'sub-1', 'r-1',
                 'virtualmachines', 60.0, 'USD', 'cost-history', now()),
                ('s1', DATE '2026-07-02', 'ActualCost', 'sub-1', 'r-2',
                 'sqldatabases', 25.0, 'USD', 'cost-history', now()),
                ('s1', DATE '2026-07-02', 'AmortizedCost', 'sub-1', 'r-1',
                 'virtualmachines', 55.0, 'USD', 'cost-history', now())
                """
            )
        result = self.database.run_semantic_query(
            SemanticQuery(
                model="daily_cost",
                measures=("total_cost", "distinct_resources"),
                dimensions=("service_name",),
                filters={"cost_type": ("ActualCost",)},
                start=date(2026, 7, 1),
                end=date(2026, 7, 31),
            )
        )
        by_service = {row[0]: row for row in result["rows"]}
        self.assertEqual(by_service["virtualmachines"][1], 100.0)
        self.assertEqual(by_service["virtualmachines"][2], 1)
        self.assertEqual(by_service["sqldatabases"][1], 25.0)
        self.assertEqual(
            [column["name"] for column in result["columns"]],
            ["service_name", "total_cost", "distinct_resources"],
        )

    def test_time_grain_produces_ordered_periods(self):
        with self.database.connect() as db:
            db.execute(
                """
                INSERT INTO daily_cost_history (
                    snapshot_id, usage_date, cost_type, subscription_id,
                    resource_id, service_name, amount, currency, source,
                    observed_at
                ) VALUES
                ('s1', DATE '2026-06-30', 'ActualCost', 's', 'r', 'svc', 10,
                 'USD', 'x', now()),
                ('s1', DATE '2026-07-01', 'ActualCost', 's', 'r', 'svc', 20,
                 'USD', 'x', now()),
                ('s1', DATE '2026-07-15', 'ActualCost', 's', 'r', 'svc', 30,
                 'USD', 'x', now())
                """
            )
        result = self.database.run_semantic_query(
            SemanticQuery(
                model="daily_cost",
                measures=("total_cost",),
                grain="month",
                filters={"cost_type": ("ActualCost",)},
            )
        )
        periods = [row[0][:10] for row in result["rows"]]
        self.assertEqual(periods, sorted(periods))
        totals = {row[0][:7]: row[1] for row in result["rows"]}
        self.assertEqual(totals["2026-06"], 10.0)
        self.assertEqual(totals["2026-07"], 50.0)

    def test_unknown_names_are_rejected(self):
        with self.assertRaises(SemanticQueryError):
            build_semantic_query(
                SemanticQuery(model="nope", measures=("total_cost",))
            )
        with self.assertRaises(SemanticQueryError):
            build_semantic_query(
                SemanticQuery(model="daily_cost", measures=("drop_table",))
            )
        with self.assertRaises(SemanticQueryError):
            build_semantic_query(
                SemanticQuery(
                    model="daily_cost",
                    measures=("total_cost",),
                    dimensions=("evil; --",),
                )
            )
        with self.assertRaises(SemanticQueryError):
            build_semantic_query(
                SemanticQuery(
                    model="governance",
                    measures=("compliance_percent",),
                    grain="day",
                )
            )

    def test_cost_type_default_prevents_double_counting(self):
        # Production charted ActualCost + AmortizedCost as one ~2x total.
        # An untouched cost_type dimension now defaults to ActualCost;
        # filtering or grouping by it opts out deliberately.
        with self.database.connect() as db:
            db.execute(
                """
                INSERT INTO daily_cost_history (
                    snapshot_id, usage_date, cost_type, subscription_id,
                    resource_id, service_name, amount, currency, source,
                    observed_at
                ) VALUES
                ('s1', DATE '2026-07-10', 'ActualCost', 's', 'r', 'svc',
                 100, 'USD', 'x', now()),
                ('s1', DATE '2026-07-10', 'AmortizedCost', 's', 'r', 'svc',
                 90, 'USD', 'x', now())
                """
            )
        defaulted = self.database.run_semantic_query(
            SemanticQuery(model="daily_cost", measures=("total_cost",))
        )
        self.assertEqual(defaulted["rows"][0][0], 100.0)
        self.assertEqual(
            defaulted["appliedDefaults"], {"cost_type": ["ActualCost"]}
        )

        explicit = self.database.run_semantic_query(
            SemanticQuery(
                model="daily_cost",
                measures=("total_cost",),
                filters={"cost_type": ("AmortizedCost",)},
            )
        )
        self.assertEqual(explicit["rows"][0][0], 90.0)
        self.assertEqual(explicit["appliedDefaults"], {})

        grouped = self.database.run_semantic_query(
            SemanticQuery(
                model="daily_cost",
                measures=("total_cost",),
                dimensions=("cost_type",),
            )
        )
        totals = {row[0]: row[1] for row in grouped["rows"]}
        self.assertEqual(totals, {"ActualCost": 100.0, "AmortizedCost": 90.0})
        self.assertEqual(grouped["appliedDefaults"], {})

    def test_completeness_lag_excludes_partial_recent_days(self):
        # Cost Management runs ~24-48h behind; the daily_cost model
        # declares a 2-day completeness lag so partial days never chart
        # as a collapse. An explicit end date overrides deliberately.
        with self.database.connect() as db:
            db.execute(
                """
                INSERT INTO daily_cost_history (
                    snapshot_id, usage_date, cost_type, subscription_id,
                    resource_id, service_name, amount, currency, source,
                    observed_at
                ) VALUES
                ('s1', current_date, 'ActualCost', 's', 'r', 'svc', 999,
                 'USD', 'x', now()),
                ('s1', current_date - INTERVAL 5 DAY, 'ActualCost', 's',
                 'r', 'svc', 111, 'USD', 'x', now())
                """
            )
        defaulted = self.database.run_semantic_query(
            SemanticQuery(model="daily_cost", measures=("total_cost",))
        )
        self.assertEqual(defaulted["rows"][0][0], 111.0)
        explicit = self.database.run_semantic_query(
            SemanticQuery(
                model="daily_cost",
                measures=("total_cost",),
                end=date.today(),
            )
        )
        self.assertEqual(explicit["rows"][0][0], 1110.0)

    def test_cost_models_expose_readable_subscription_names(self):
        # Cost and anomaly rows carry only the subscription GUID. Grouping
        # or filtering by it in the explorer produced a wall of unreadable
        # ids, so both models resolve the friendly name and fall back to
        # the id when a subscription has no inventory.
        for name in ("daily_cost", "cost_anomalies"):
            dimensions = [d.name for d in find_model(name).dimensions]
            self.assertIn("subscription_name", dimensions, name)
            self.assertLess(
                dimensions.index("subscription_name"),
                dimensions.index("subscription_id"),
                f"{name}: the readable name should be offered first",
            )

        with self.database.connect() as db:
            db.execute(
                """
                INSERT INTO daily_cost_history (
                    snapshot_id, usage_date, cost_type, subscription_id,
                    resource_id, service_name, amount, currency, source,
                    observed_at
                ) VALUES
                ('s1', current_date - INTERVAL 5 DAY, 'ActualCost',
                 '00000000-0000-0000-0000-000000000001', 'r-1', 'svc',
                 120.0, 'USD', 'x', now()),
                ('s1', current_date - INTERVAL 5 DAY, 'ActualCost',
                 'no-such-subscription', 'r-2', 'svc',
                 30.0, 'USD', 'x', now())
                """
            )
        result = self.database.run_semantic_query(
            SemanticQuery(
                model="daily_cost",
                measures=("total_cost",),
                dimensions=("subscription_name",),
            )
        )
        totals = {row[0]: row[1] for row in result["rows"]}
        self.assertIn("Platform Production", totals)
        self.assertEqual(totals["Platform Production"], 120.0)
        # Unknown subscription degrades to its id rather than vanishing.
        self.assertEqual(totals["no-such-subscription"], 30.0)

    def test_snapshot_older_than_registry_degrades_cleanly(self):
        # Code deploys ahead of data. A snapshot built before a dimension
        # was added lacks its column; production returned HTTP 500 from an
        # opaque DuckDB binder error about GROUP BY aliases. The catalog
        # must hide such a dimension and the query must fail readably.
        with self.database.connect() as db:
            db.execute(
                """
                CREATE OR REPLACE VIEW semantic_daily_cost AS
                SELECT usage_date, cost_type, subscription_id, resource_id,
                       service_name, amount, currency, source
                FROM daily_cost_history
                """
            )
        catalog = self.database.semantic_catalog()
        daily = next(
            m for m in catalog["models"] if m["name"] == "daily_cost"
        )
        offered = [d["name"] for d in daily["dimensions"]]
        self.assertNotIn("subscription_name", offered)
        self.assertIn("subscription_id", offered)

        with self.assertRaises(SemanticQueryError) as caught:
            self.database.run_semantic_query(
                SemanticQuery(
                    model="daily_cost",
                    measures=("total_cost",),
                    dimensions=("subscription_name",),
                    grain="day",
                )
            )
        self.assertIn("snapshot", str(caught.exception).lower())

        # Rebuilding the views restores the dimension.
        with self.database.connect() as db:
            create_semantic_views(db)
        catalog = self.database.semantic_catalog()
        daily = next(
            m for m in catalog["models"] if m["name"] == "daily_cost"
        )
        self.assertIn(
            "subscription_name", [d["name"] for d in daily["dimensions"]]
        )

    def test_filter_values_travel_as_parameters(self):
        malicious = "x'); DROP TABLE daily_cost_history; --"
        result = self.database.run_semantic_query(
            SemanticQuery(
                model="daily_cost",
                measures=("cost_rows",),
                filters={"service_name": (malicious,)},
            )
        )
        self.assertEqual(result["rows"][0][0], 0)
        with self.database.connect(read_only=True) as db:
            db.execute("SELECT COUNT(*) FROM daily_cost_history")

    def test_catalog_reports_availability(self):
        catalog = self.database.semantic_catalog()
        availability = {
            model["name"]: model["available"] for model in catalog["models"]
        }
        self.assertTrue(all(availability.values()), availability)
        self.assertIsNotNone(find_model("workload_optimization"))


if __name__ == "__main__":
    unittest.main()


class RillProjectParityTests(unittest.TestCase):
    def test_committed_rill_project_matches_registry(self):
        # rill/ is generated from the registry; a mismatch means someone
        # edited YAML by hand or forgot to rerun the generator after a
        # registry change.
        import sys
        from pathlib import Path as _Path

        sys.path.insert(0, str(_Path("scripts").resolve().parent))
        from scripts.generate_rill_project import generated_files

        for path, expected in generated_files().items():
            self.assertTrue(path.exists(), f"missing generated file: {path}")
            self.assertEqual(
                path.read_text(encoding="utf-8").replace("\r\n", "\n"),
                expected,
                f"stale generated file: {path}; rerun "
                "scripts/generate_rill_project.py",
            )
