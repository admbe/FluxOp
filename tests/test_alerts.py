from datetime import datetime, timedelta, timezone
import unittest

from api.alerts import WarningState, select_notifications, warning_key


class AlertSelectionTests(unittest.TestCase):
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

    def test_new_warning_is_sent_and_becomes_active(self):
        to_send, active = select_notifications(
            ["3 syncs queued and unclaimed for up to 20 minutes"],
            {},
            self.now,
        )
        self.assertEqual(len(to_send), 1)
        self.assertIn("unclaimed", to_send[0][1])
        self.assertEqual(active, [to_send[0][0]])

    def test_known_recent_warning_is_not_resent(self):
        text = "3 syncs queued and unclaimed for up to 20 minutes"
        key = warning_key(text)
        known = {
            key: WarningState(
                key, self.now - timedelta(hours=5), self.now - timedelta(hours=2)
            )
        }
        to_send, active = select_notifications([text], known, self.now)
        self.assertEqual(to_send, [])
        self.assertEqual(active, [key])

    def test_persistent_warning_renotifies_after_cadence(self):
        text = "The newest approved analytical snapshot is 30 hours old."
        key = warning_key(text)
        known = {
            key: WarningState(
                key,
                self.now - timedelta(days=2),
                self.now - timedelta(hours=25),
            )
        }
        to_send, _ = select_notifications([text], known, self.now)
        self.assertEqual(len(to_send), 1)
        self.assertEqual(to_send[0][0], key)
        self.assertIn("Still active since 2026-07-31", to_send[0][1])

    def test_changing_figures_do_not_change_identity(self):
        self.assertEqual(
            warning_key("queued and unclaimed for up to 20 minutes"),
            warning_key("queued and unclaimed for up to 95 minutes"),
        )
        self.assertNotEqual(
            warning_key("Triggered WebJob 'flux-cost-history' holds a lock"),
            warning_key("Triggered WebJob 'flux-advisor' holds a lock"),
        )

    def test_resolved_warning_absent_from_active_set(self):
        to_send, active = select_notifications([], {"k": WarningState(
            "k", self.now, self.now
        )}, self.now)
        self.assertEqual(to_send, [])
        self.assertEqual(active, [])


if __name__ == "__main__":
    unittest.main()
