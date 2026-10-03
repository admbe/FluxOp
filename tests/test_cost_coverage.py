"""Day-level cost-history completeness ledger and gap requeue behavior."""

from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase, utc_now


AS_OF = date(2026, 6, 20)
WINDOW_DAYS = 30


class CostCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "coverage.duckdb")
        self.database.init()
        self.database.save_integration(
            {
                "name": "Azure",
                "tenantId": "tenant",
                "enabled": True,
                "authMode": "managed_identity",
                "subscriptions": [
                    {"subscriptionId": "sub-1", "label": "Production"},
                    {"subscriptionId": "sub-2", "label": "Development"},
                ],
            }
        )

    def tearDown(self):
        self.temp.cleanup()

    def _seed_days(self, subscription_id, cost_type, days):
        records = [
            {
                "usageDate": day.isoformat(),
                "costType": cost_type,
                "subscriptionId": subscription_id,
                "resourceId": f"/r/{subscription_id}",
                "serviceName": "Virtual Machines",
                "amount": 10.0,
                "currency": "USD",
                "source": "test",
            }
            for day in days
        ]
        self.database.store_daily_cost_scope(
            f"seed-{subscription_id}-{cost_type}",
            subscription_id,
            cost_type,
            records,
            start_date=min(days),
            end_date=max(days),
        )

    def _window(self):
        finalized_end = AS_OF - timedelta(days=2)
        window_start = AS_OF - timedelta(days=WINDOW_DAYS - 1)
        return window_start, finalized_end

    def test_ledger_reports_gaps_and_estate_rollup(self):
        window_start, finalized_end = self._window()
        # sub-1 ActualCost: full window except a 3-day hole in the middle.
        hole = {
            window_start + timedelta(days=10),
            window_start + timedelta(days=11),
            window_start + timedelta(days=12),
        }
        days = [
            day
            for offset in range((finalized_end - window_start).days + 1)
            if (day := window_start + timedelta(days=offset)) not in hole
        ]
        self._seed_days("sub-1", "ActualCost", days)

        coverage = self.database.cost_history_coverage(
            initial_days=WINDOW_DAYS, as_of=AS_OF
        )
        # 2 subscriptions x 2 cost types.
        self.assertEqual(coverage["scopeCount"], 4)
        expected_days = (finalized_end - window_start).days + 1
        by_key = {
            (scope["subscriptionId"], scope["costType"]): scope
            for scope in coverage["scopes"]
        }
        seeded = by_key[("sub-1", "ActualCost")]
        self.assertEqual(seeded["expectedDays"], expected_days)
        self.assertEqual(seeded["missingDays"], 3)
        self.assertEqual(
            seeded["missingRanges"],
            [
                {
                    "start": (window_start + timedelta(days=10)).isoformat(),
                    "end": (window_start + timedelta(days=12)).isoformat(),
                }
            ],
        )
        # A configured scope with no data at all is fully missing, not absent.
        empty = by_key[("sub-2", "AmortizedCost")]
        self.assertEqual(empty["ingestedDays"], 0)
        self.assertEqual(empty["coveragePercent"], 0.0)
        self.assertEqual(len(empty["missingRanges"]), 1)
        # Estate rollup counts scope-days across all four scopes.
        self.assertEqual(
            coverage["expectedScopeDays"], expected_days * 4
        )
        self.assertEqual(
            coverage["ingestedScopeDays"], expected_days - 3
        )
        # Worst coverage sorts first for the UI.
        self.assertEqual(coverage["scopes"][0]["coveragePercent"], 0.0)

    def test_requeue_flips_succeeded_months_and_respects_bounds(self):
        window_start, finalized_end = self._window()
        # Seed everything except one hole inside the month of the window
        # start, then mark that month's backfill checkpoint as succeeded --
        # the state the failure-driven fallback never revisits.
        gap_month = window_start.replace(day=1)
        hole_start = window_start + timedelta(days=2)
        days = [
            day
            for offset in range((finalized_end - window_start).days + 1)
            if not (
                hole_start
                <= (day := window_start + timedelta(days=offset))
                <= hole_start + timedelta(days=1)
            )
        ]
        for subscription in ("sub-1", "sub-2"):
            for cost_type in ("ActualCost", "AmortizedCost"):
                self._seed_days(subscription, cost_type, days)
        self.database.begin_cost_details_backfill(
            "sub-1", "ActualCost", gap_month, window_start + timedelta(days=5)
        )
        self.database.finish_cost_details_backfill(
            "sub-1",
            "ActualCost",
            gap_month,
            status="succeeded",
            row_count=10,
        )
        # Age the attempt beyond the requeue cooldown.
        with self.database.operational_connect() as db:
            db.execute(
                """
                UPDATE cost_details_backfill_scopes
                SET last_attempt_at = ?
                """,
                [utc_now() - timedelta(days=10)],
            )

        result = self.database.requeue_cost_coverage_gaps(
            initial_days=WINDOW_DAYS, as_of=AS_OF, limit=2
        )
        self.assertEqual(len(result["requeued"]), 2)
        self.assertGreater(result["candidateMonths"], 2)
        first = result["requeued"][0]
        self.assertEqual(first["periodStart"], gap_month.isoformat())
        with self.database.operational_connect(read_only=True) as db:
            status = db.execute(
                """
                SELECT status FROM cost_details_backfill_scopes
                WHERE subscription_id = 'sub-1' AND cost_type = 'ActualCost'
                  AND period_start = ?
                """,
                [gap_month],
            ).fetchone()[0]
        self.assertEqual(status, "gap")
        # The collector walks months newest-first; once the current month has
        # a fresh succeeded checkpoint, the flipped month is what it fetches
        # next -- a succeeded checkpoint no longer hides the hole behind it.
        current_month = AS_OF.replace(day=1)
        self.database.begin_cost_details_backfill(
            "sub-1", "ActualCost", current_month, AS_OF
        )
        self.database.finish_cost_details_backfill(
            "sub-1", "ActualCost", current_month, status="succeeded",
            row_count=1,
        )
        period = self.database.next_cost_details_backfill_period(
            "sub-1",
            "ActualCost",
            initial_days=WINDOW_DAYS,
            current_refresh_days=7,
            as_of=AS_OF,
        )
        self.assertIsNotNone(period)
        self.assertEqual(period[0], gap_month)
        # Simulating a fresh attempt re-arms the cooldown, so an immediate
        # second planning pass does not hammer the same month.
        self.database.begin_cost_details_backfill(
            "sub-1", "ActualCost", gap_month, window_start + timedelta(days=5)
        )
        again = self.database.requeue_cost_coverage_gaps(
            initial_days=WINDOW_DAYS, as_of=AS_OF, limit=10
        )
        self.assertNotIn(
            ("sub-1", "ActualCost", gap_month.isoformat()),
            [
                (
                    item["subscriptionId"],
                    item["costType"],
                    item["periodStart"],
                )
                for item in again["requeued"]
            ],
        )

    def test_unsupported_months_are_never_requeued(self):
        window_start, _ = self._window()
        gap_month = window_start.replace(day=1)
        self.database.begin_cost_details_backfill(
            "sub-1", "ActualCost", gap_month, window_start
        )
        self.database.finish_cost_details_backfill(
            "sub-1", "ActualCost", gap_month, status="unsupported"
        )
        with self.database.operational_connect() as db:
            db.execute(
                "UPDATE cost_details_backfill_scopes SET last_attempt_at = ?",
                [utc_now() - timedelta(days=30)],
            )
        result = self.database.requeue_cost_coverage_gaps(
            initial_days=WINDOW_DAYS, as_of=AS_OF, limit=50
        )
        self.assertNotIn(
            gap_month.isoformat(),
            [
                item["periodStart"]
                for item in result["requeued"]
                if item["subscriptionId"] == "sub-1"
                and item["costType"] == "ActualCost"
            ],
        )

    def test_cost_report_disclosure_and_scope_filter(self):
        window_start, finalized_end = self._window()
        full = [
            window_start + timedelta(days=offset)
            for offset in range((finalized_end - window_start).days + 1)
        ]
        self._seed_days("sub-1", "ActualCost", full)
        # sub-2 ingested nothing: half the estate's subscription-days missing.
        report = self.database.cost_report(
            cost_type="ActualCost",
            start_date=window_start,
            end_date=finalized_end,
        )
        coverage = report["period"]["coverage"]
        self.assertEqual(coverage["expectedScopeDays"], len(full) * 2)
        self.assertEqual(coverage["ingestedScopeDays"], len(full))
        self.assertIn("subscription-days", report["period"]["note"])
        # Filtered to the fully ingested subscription: no gap, no note text
        # about coverage shortfall.
        filtered = self.database.cost_report(
            cost_type="ActualCost",
            start_date=window_start,
            end_date=finalized_end,
            subscription_id="sub-1",
        )
        self.assertEqual(
            filtered["period"]["coverage"]["coveragePercent"], 100.0
        )
        # Service-level views never claim coverage: absence of rows for a
        # service is legitimately zero spend, not missing ingestion.
        service = self.database.cost_report(
            cost_type="ActualCost",
            start_date=window_start,
            end_date=finalized_end,
            service_name="Virtual Machines",
        )
        self.assertIsNone(service["period"]["coverage"])

    def _record_empty_query(
        self, subscription_id, cost_type, query_start, query_end
    ):
        """A succeeded collection that returned nothing: Azure was asked and
        answered that there is no spend in the window."""
        run_id = f"run-{subscription_id}-{cost_type}-{query_start}"
        self.database.begin_cost_history_scope(
            run_id, subscription_id, cost_type, query_start, query_end
        )
        self.database.finish_cost_history_scope(
            run_id,
            subscription_id,
            cost_type,
            status="succeeded",
            row_count=0,
        )

    def _record_blocked_scope(self, subscription_id, cost_type):
        window_start, finalized_end = self._window()
        run_id = f"blocked-{subscription_id}-{cost_type}"
        self.database.begin_cost_history_scope(
            run_id, subscription_id, cost_type, window_start, finalized_end
        )
        self.database.finish_cost_history_scope(
            run_id,
            subscription_id,
            cost_type,
            status="failed",
            status_code=401,
            message="Cost Management returned HTTP 401",
        )

    def test_verified_empty_days_count_as_covered_not_missing(self):
        window_start, finalized_end = self._window()
        # sub-2 AmortizedCost has no spend at all, and a succeeded query
        # proved it across the whole window.
        self._record_empty_query(
            "sub-2", "AmortizedCost", window_start, finalized_end
        )
        coverage = self.database.cost_history_coverage(
            initial_days=WINDOW_DAYS, as_of=AS_OF
        )
        by_key = {
            (scope["subscriptionId"], scope["costType"]): scope
            for scope in coverage["scopes"]
        }
        expected_days = (finalized_end - window_start).days + 1
        empty = by_key[("sub-2", "AmortizedCost")]
        self.assertEqual(empty["missingDays"], 0)
        self.assertEqual(empty["verifiedEmptyDays"], expected_days)
        self.assertEqual(empty["ingestedDays"], 0)
        self.assertEqual(empty["coveragePercent"], 100.0)
        # Ingested percent still tells the raw story for anyone who needs it.
        self.assertEqual(empty["ingestedPercent"], 0.0)
        self.assertEqual(empty["missingRanges"], [])
        # A scope with nothing at all and no proof stays missing.
        unproven = by_key[("sub-1", "ActualCost")]
        self.assertEqual(unproven["missingDays"], expected_days)
        self.assertEqual(unproven["verifiedEmptyDays"], 0)

    def test_ingested_days_win_over_a_stale_empty_window(self):
        window_start, finalized_end = self._window()
        # An older zero-row window covering days that later received rows
        # must not demote them to "empty".
        self._record_empty_query(
            "sub-1", "ActualCost", window_start, finalized_end
        )
        days = [window_start + timedelta(days=offset) for offset in range(5)]
        self._seed_days("sub-1", "ActualCost", days)
        coverage = self.database.cost_history_coverage(
            initial_days=WINDOW_DAYS, as_of=AS_OF
        )
        scope = next(
            item for item in coverage["scopes"]
            if (item["subscriptionId"], item["costType"])
            == ("sub-1", "ActualCost")
        )
        expected_days = (finalized_end - window_start).days + 1
        self.assertEqual(scope["ingestedDays"], len(days))
        self.assertEqual(
            scope["verifiedEmptyDays"], expected_days - len(days)
        )
        self.assertEqual(scope["missingDays"], 0)

    def test_blocked_scopes_leave_the_estate_totals(self):
        window_start, finalized_end = self._window()
        self._record_blocked_scope("sub-2", "ActualCost")
        coverage = self.database.cost_history_coverage(
            initial_days=WINDOW_DAYS, as_of=AS_OF
        )
        expected_days = (finalized_end - window_start).days + 1
        self.assertEqual(coverage["scopeCount"], 4)
        self.assertEqual(coverage["blockedScopes"], 1)
        self.assertEqual(coverage["reportingScopeCount"], 4)
        # The blocked scope's days are out of the denominator, so a fully
        # collecting estate can still reach 100%.
        self.assertEqual(coverage["expectedScopeDays"], expected_days * 4)
        blocked = next(
            item for item in coverage["scopes"] if item["blocked"]
        )
        self.assertIn("401", blocked["blockedReason"])

    def test_verified_empty_and_blocked_scopes_are_never_requeued(self):
        window_start, finalized_end = self._window()
        self._record_empty_query(
            "sub-1", "ActualCost", window_start, finalized_end
        )
        self._record_empty_query(
            "sub-1", "AmortizedCost", window_start, finalized_end
        )
        self._record_blocked_scope("sub-2", "ActualCost")
        result = self.database.requeue_cost_coverage_gaps(
            initial_days=WINDOW_DAYS, as_of=AS_OF, limit=3
        )
        requeued = {
            (item["subscriptionId"], item["costType"])
            for item in result["requeued"]
        }
        self.assertNotIn(("sub-1", "ActualCost"), requeued)
        self.assertNotIn(("sub-1", "AmortizedCost"), requeued)
        self.assertNotIn(("sub-2", "ActualCost"), requeued)
        # The one scope with a genuine, unexplained gap is still eligible.
        self.assertEqual(
            requeued, {("sub-2", "AmortizedCost")}
        )

    def test_anomaly_baseline_coverage_math(self):
        baseline_end = AS_OF - timedelta(days=2)
        baseline_start = baseline_end - timedelta(days=56)
        days = [
            baseline_start + timedelta(days=offset)
            for offset in range(0, 57, 2)  # every other day: ~50% coverage
        ]
        self._seed_days("sub-1", "AmortizedCost", days)
        coverage = self.database._anomaly_baseline_coverage(
            "AmortizedCost", as_of=AS_OF
        )
        self.assertEqual(coverage["expectedScopeDays"], 57 * 2)
        self.assertEqual(coverage["ingestedScopeDays"], len(days))
        self.assertLess(coverage["coveragePercent"], 60.0)

