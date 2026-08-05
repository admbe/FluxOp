"""SLO evaluation bands and the notify-on-transition state machine."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase
from api.slo import (
    DEFAULT_SLOS,
    SloState,
    apply_threshold_overrides,
    evaluate_slo,
    select_slo_transitions,
)


def definition(key):
    return next(item for item in DEFAULT_SLOS if item.key == key)


class SloEvaluationTests(unittest.TestCase):
    def test_below_direction_bands(self):
        coverage = definition("cost_coverage_percent")
        self.assertEqual(evaluate_slo(coverage, 99.5)["state"], "ok")
        self.assertEqual(evaluate_slo(coverage, 97.0)["state"], "warn")
        self.assertEqual(evaluate_slo(coverage, 85.0)["state"], "breach")
        self.assertEqual(evaluate_slo(coverage, None)["state"], "unknown")

    def test_above_direction_bands(self):
        snapshot = definition("snapshot_age_hours")
        self.assertEqual(evaluate_slo(snapshot, 2.0)["state"], "ok")
        self.assertEqual(evaluate_slo(snapshot, 40.0)["state"], "warn")
        self.assertEqual(evaluate_slo(snapshot, 60.0)["state"], "breach")

    def test_threshold_overrides_change_bands_only(self):
        adjusted = apply_threshold_overrides(
            DEFAULT_SLOS,
            {"cost_coverage_percent": {"warn": 99.5, "breach": 95}},
        )
        coverage = next(
            item for item in adjusted if item.key == "cost_coverage_percent"
        )
        self.assertEqual(coverage.warn, 99.5)
        self.assertEqual(coverage.breach, 95.0)
        self.assertEqual(evaluate_slo(coverage, 99.0)["state"], "warn")
        # Untouched objectives keep their defaults.
        self.assertEqual(definition("snapshot_age_hours").warn, 30.0)


class SloTransitionTests(unittest.TestCase):
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

    def evaluations(self, coverage="absent"):
        definition_ = definition("cost_coverage_percent")
        value = None if coverage == "absent" else coverage
        return [evaluate_slo(definition_, value)]

    def test_new_breach_notifies_and_persists_state(self):
        messages, upserts, clears = select_slo_transitions(
            self.evaluations(coverage=80.0), {}, self.now
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("breach", messages[0])
        self.assertIn("cost-history-reliability", messages[0])
        self.assertEqual(
            upserts,
            [("cost_coverage_percent", "breach", self.now, self.now)],
        )
        self.assertEqual(clears, [])

    def test_persistent_state_is_silent_within_cadence(self):
        known = {
            "cost_coverage_percent": SloState(
                "cost_coverage_percent",
                "breach",
                self.now - timedelta(hours=5),
                self.now - timedelta(hours=5),
            )
        }
        messages, upserts, _ = select_slo_transitions(
            self.evaluations(coverage=80.0), known, self.now
        )
        self.assertEqual(messages, [])
        # State row survives untouched so `since` stays honest.
        self.assertEqual(upserts[0][2], self.now - timedelta(hours=5))

    def test_persistent_state_renotifies_after_cadence(self):
        known = {
            "cost_coverage_percent": SloState(
                "cost_coverage_percent",
                "breach",
                self.now - timedelta(days=3),
                self.now - timedelta(hours=25),
            )
        }
        messages, upserts, _ = select_slo_transitions(
            self.evaluations(coverage=80.0), known, self.now
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("Still breach", messages[0])
        # since is preserved; last_notified advances.
        self.assertEqual(upserts[0][2], self.now - timedelta(days=3))
        self.assertEqual(upserts[0][3], self.now)

    def test_worsening_notifies_immediately(self):
        known = {
            "cost_coverage_percent": SloState(
                "cost_coverage_percent",
                "warn",
                self.now - timedelta(hours=1),
                self.now - timedelta(hours=1),
            )
        }
        messages, upserts, _ = select_slo_transitions(
            self.evaluations(coverage=80.0), known, self.now
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("SLO breach", messages[0])
        self.assertEqual(upserts[0][1], "breach")

    def test_improvement_within_bad_states_notes_once(self):
        known = {
            "cost_coverage_percent": SloState(
                "cost_coverage_percent",
                "breach",
                self.now - timedelta(hours=4),
                self.now - timedelta(hours=4),
            )
        }
        messages, upserts, _ = select_slo_transitions(
            self.evaluations(coverage=95.0), known, self.now
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("Improved to warn", messages[0])
        self.assertEqual(upserts[0][1], "warn")

    def test_recovery_notifies_once_and_clears(self):
        known = {
            "cost_coverage_percent": SloState(
                "cost_coverage_percent",
                "breach",
                self.now - timedelta(days=1),
                self.now - timedelta(hours=2),
            )
        }
        messages, upserts, clears = select_slo_transitions(
            self.evaluations(coverage=99.9), known, self.now
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("Recovered", messages[0])
        self.assertEqual(clears, ["cost_coverage_percent"])
        self.assertEqual(upserts, [])
        # Second pass with no tracked state: silence.
        again = select_slo_transitions(
            self.evaluations(coverage=99.9), {}, self.now
        )
        self.assertEqual(again, ([], [], []))

    def test_unknown_holds_state_and_never_notifies(self):
        known = {
            "cost_coverage_percent": SloState(
                "cost_coverage_percent",
                "breach",
                self.now - timedelta(days=1),
                self.now - timedelta(hours=2),
            )
        }
        messages, upserts, clears = select_slo_transitions(
            self.evaluations(), known, self.now
        )
        self.assertEqual(messages, [])
        self.assertEqual(clears, [])
        # Previous breach state is carried, not recovered, while blind.
        self.assertEqual(upserts[0][1], "breach")
        # Unknown with no history stays invisible.
        fresh = select_slo_transitions(self.evaluations(), {}, self.now)
        self.assertEqual(fresh, ([], [], []))


class SloReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "slo.duckdb")
        self.database.init()

    def tearDown(self):
        self.temp.cleanup()

    def test_report_shape_and_unknown_probes(self):
        report = self.database.slo_report(initial_days=30)
        keys = {item["key"] for item in report["objectives"]}
        self.assertEqual(
            keys,
            {
                "cost_coverage_percent",
                "cost_scope_success_percent",
                "stale_source_count",
                "snapshot_age_hours",
            },
        )
        by_key = {item["key"]: item for item in report["objectives"]}
        # Empty database: no scope runs and no snapshot exist yet, which
        # must read as unknown, never as ok.
        self.assertEqual(
            by_key["cost_scope_success_percent"]["state"], "unknown"
        )
        self.assertEqual(by_key["snapshot_age_hours"]["state"], "unknown")
        self.assertIn(
            report["worstState"], {"ok", "unknown", "warn", "breach"}
        )

    def test_coverage_measurement_flows_from_ledger(self):
        self.database.save_integration(
            {
                "name": "Azure",
                "tenantId": "tenant",
                "enabled": True,
                "authMode": "managed_identity",
                "subscriptions": [
                    {"subscriptionId": "sub-1", "label": "Production"}
                ],
            }
        )
        measurements = self.database.slo_measurements(initial_days=30)
        # Configured scopes with zero ingested days: 0% coverage, a breach
        # signal, not an unknown.
        self.assertEqual(measurements["cost_coverage_percent"], 0.0)
