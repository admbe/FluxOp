import dataclasses
from datetime import date
import unittest
from unittest.mock import patch

import api.jobs as jobs
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



def _publishing_enabled(enabled: bool = True):
    """Settings is a frozen dataclass, so swap the object rather than setattr."""
    return patch.object(
        jobs,
        "settings",
        dataclasses.replace(jobs.settings, analytics_snapshot_publish=enabled),
    )


class SnapshotPublicationExitCodeTests(unittest.TestCase):
    """A rejected snapshot must not be reported as a successful job.

    publish_analytics_snapshot() returns 1 when publisher.publish() returns
    None, and publish() returns None precisely when validate_candidate or
    _reject_catastrophic_regression rejected the candidate -- i.e. when the
    corruption detector fired. _with_publication discarded that result, so
    every data-writing WebJob exited 0, Kudu stayed green, and the web served
    the last good snapshot indefinitely while users saw confident cost
    figures that had quietly stopped advancing.
    """

    def test_rejected_publication_surfaces_a_distinct_exit_code(self):
        with _publishing_enabled(), \
                patch.object(jobs, "publish_analytics_snapshot", return_value=1):
            self.assertEqual(
                jobs._with_publication(0), jobs.SNAPSHOT_PUBLICATION_REJECTED
            )

    def test_publication_exception_surfaces_a_distinct_exit_code(self):
        with _publishing_enabled(), \
                patch.object(jobs, "publish_analytics_snapshot",
                             side_effect=RuntimeError("storage down")):
            self.assertEqual(
                jobs._with_publication(0), jobs.SNAPSHOT_PUBLICATION_REJECTED
            )

    def test_successful_publication_preserves_the_job_exit_code(self):
        with _publishing_enabled(), \
                patch.object(jobs, "publish_analytics_snapshot", return_value=0):
            self.assertEqual(jobs._with_publication(0), 0)

    def test_a_failing_job_is_never_masked_by_publication(self):
        with _publishing_enabled(), \
                patch.object(jobs, "publish_analytics_snapshot", return_value=0):
            self.assertEqual(jobs._with_publication(1), 1)

    def test_publication_disabled_is_a_passthrough(self):
        with _publishing_enabled(False):
            self.assertEqual(jobs._with_publication(0), 0)


if __name__ == "__main__":
    unittest.main()
