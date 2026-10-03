"""The overview card's month-to-date window, especially at month boundaries.

The bug these pin: the window derived its month from the FINALIZED END
rather than from today. Because that horizon sits two days back,
``end.replace(day=1)`` landed in the PRIOR month on the 1st and 2nd -- on
2026-09-01 the card reported 2026-08-01..08-30, a near-complete August, as
"September month to date". Verified pre-fix against seeded data: the card
returned $30,000 of July spend as August MTD on 2026-08-01.

budget_report hit the same period inversion and fixed it in c617ca3; this
brings the overview card onto the same anchoring.
"""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase


def seeded_database(tmp: str, start: date, days: int) -> FluxDatabase:
    """A database with one $1,000 ActualCost row per day from ``start``."""
    database = FluxDatabase(Path(tmp) / "flux.duckdb")
    database.init()
    observed = datetime.now(timezone.utc)
    with database.connect() as db:
        for offset in range(days):
            db.execute(
                """
                INSERT INTO daily_cost_history VALUES (
                    'seed', ?, ?, 'ActualCost', 'sub-a',
                    '/id/res-1', 'Compute', 1000.0, 'USD', 'test'
                )
                """,
                [observed, start + timedelta(days=offset)],
            )
        db.commit()
    return database


class OverviewMonthBoundaryTests(unittest.TestCase):
    def test_first_of_month_does_not_report_last_month_as_this_month(self):
        """The exact regression: July's total shown as August MTD."""
        with TemporaryDirectory() as tmp:
            database = seeded_database(tmp, date(2026, 7, 1), 31)
            card = database._overview_period_comparison(
                as_of=date(2026, 8, 1)
            )
        # July holds $31,000. Pre-fix this returned $30,000 of it with
        # mtdStart 2026-07-01. Now there is simply nothing finalized yet.
        if card is not None:
            self.assertTrue(
                card["mtdStart"].startswith("2026-08"),
                f"August card anchored on {card['mtdStart']}",
            )
            self.assertEqual(card["mtdActual"], 0.0)

    def test_second_of_month_likewise(self):
        with TemporaryDirectory() as tmp:
            database = seeded_database(tmp, date(2026, 7, 1), 31)
            card = database._overview_period_comparison(
                as_of=date(2026, 8, 2)
            )
        if card is not None:
            self.assertTrue(card["mtdStart"].startswith("2026-08"))
            self.assertEqual(card["mtdActual"], 0.0)

    def test_third_of_month_opens_exactly_one_finalized_day(self):
        with TemporaryDirectory() as tmp:
            database = seeded_database(tmp, date(2026, 7, 1), 40)
            card = database._overview_period_comparison(
                as_of=date(2026, 8, 3)
            )
        self.assertIsNotNone(card)
        self.assertEqual(card["mtdStart"], "2026-08-01")
        self.assertEqual(card["mtdEnd"], "2026-08-01")
        self.assertEqual(card["mtdActual"], 1000.0)
        # Same one-day span on the prior side, not a whole month.
        self.assertEqual(card["priorStart"], "2026-07-01")
        self.assertEqual(card["priorEnd"], "2026-07-01")
        self.assertEqual(card["priorMtdActual"], 1000.0)

    def test_mid_month_compares_equal_day_spans(self):
        with TemporaryDirectory() as tmp:
            database = seeded_database(tmp, date(2026, 7, 1), 45)
            card = database._overview_period_comparison(
                as_of=date(2026, 8, 11)
            )
        self.assertIsNotNone(card)
        self.assertEqual(card["mtdStart"], "2026-08-01")
        self.assertEqual(card["mtdEnd"], "2026-08-09")
        self.assertEqual(card["priorStart"], "2026-07-01")
        self.assertEqual(card["priorEnd"], "2026-07-09")
        self.assertEqual(card["mtdActual"], 9000.0)
        self.assertEqual(card["priorMtdActual"], 9000.0)

    def test_window_month_never_diverges_across_a_full_year(self):
        """Property: for every day of 2026, the window stays in its month."""
        with TemporaryDirectory() as tmp:
            database = seeded_database(tmp, date(2025, 12, 1), 400)
            day = date(2026, 1, 1)
            while day < date(2027, 1, 1):
                card = database._overview_period_comparison(as_of=day)
                if card is None:
                    # Only legitimate before any day of the month finalizes.
                    self.assertLessEqual(
                        day.day, 2, f"{day}: card vanished mid-month"
                    )
                else:
                    self.assertEqual(
                        card["mtdStart"], day.replace(day=1).isoformat(),
                        f"{day}: window anchored on {card['mtdStart']}",
                    )
                day += timedelta(days=1)


if __name__ == "__main__":
    unittest.main()
