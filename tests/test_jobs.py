from datetime import date
import unittest

from api.jobs import _cost_history_windows


class CostHistoryJobTests(unittest.TestCase):
    def test_cost_history_windows_are_bounded_contiguous_and_newest_first(self):
        windows = _cost_history_windows(
            date(2026, 4, 28),
            date(2026, 7, 26),
            14,
        )

        self.assertEqual(windows[0], (date(2026, 7, 13), date(2026, 7, 26)))
        self.assertEqual(windows[-1], (date(2026, 4, 28), date(2026, 5, 3)))
        self.assertTrue(
            all((end - start).days < 14 for start, end in windows)
        )
        for newer, older in zip(windows, windows[1:]):
            self.assertEqual(older[1].toordinal() + 1, newer[0].toordinal())

    def test_cost_history_window_size_is_never_less_than_one_day(self):
        self.assertEqual(
            _cost_history_windows(
                date(2026, 7, 25),
                date(2026, 7, 26),
                0,
            ),
            [
                (date(2026, 7, 26), date(2026, 7, 26)),
                (date(2026, 7, 25), date(2026, 7, 25)),
            ],
        )


if __name__ == "__main__":
    unittest.main()
