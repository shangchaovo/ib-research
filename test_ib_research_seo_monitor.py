import unittest
from unittest.mock import patch

import ib_research_seo_monitor as monitor


class MonitorStateTests(unittest.TestCase):
    def setUp(self):
        self.results = {"home": (False, "unreachable (direct: ConnectTimeout)")}

    @patch.object(monitor, "FAILURES_BEFORE_ALERT", 2)
    def test_requires_consecutive_failures(self):
        message, state = monitor._notification_for_state(
            ["home"], self.results, {}, 100.0
        )
        self.assertIsNone(message)
        self.assertEqual(state["consecutive_failures"], 1)

        message, state = monitor._notification_for_state(
            ["home"], self.results, state, 200.0
        )
        self.assertIn("连续失败 2 次", message)
        self.assertTrue(state["incident_alerted"])

    @patch.object(monitor, "FAILURES_BEFORE_ALERT", 2)
    @patch.object(monitor, "REMINDER_SECONDS", 86400)
    def test_failure_set_churn_does_not_repeat_alert(self):
        state = {
            "status": "failing",
            "consecutive_failures": 2,
            "incident_alerted": True,
            "last_alert_ts": 100.0,
        }
        changed_results = {
            **self.results,
            "robots": (False, "http 530"),
        }
        message, state = monitor._notification_for_state(
            ["home", "robots"], changed_results, state, 300.0
        )
        self.assertIsNone(message)
        self.assertEqual(state["consecutive_failures"], 3)

    def test_recovery_only_notifies_after_an_alert(self):
        message, _ = monitor._notification_for_state(
            [], {}, {"status": "failing", "incident_alerted": False}, 200.0
        )
        self.assertIsNone(message)

        message, state = monitor._notification_for_state(
            [], {}, {"status": "failing", "incident_alerted": True}, 300.0
        )
        self.assertIn("恢复正常", message)
        self.assertEqual(state["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
