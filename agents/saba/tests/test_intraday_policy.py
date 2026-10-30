import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from market_data.intraday_policy import IntradayPolicy, assess_late_session

TZ = ZoneInfo("Europe/Stockholm")


class IntradayPolicyTests(unittest.TestCase):
    def test_time_feasible(self):
        a = assess_late_session(datetime(2026, 10, 30, 20, 45, tzinfo=TZ), 20, 5)
        self.assertEqual(a.decision, "TIME_FEASIBLE")

    def test_reject_too_long(self):
        a = assess_late_session(datetime(2026, 10, 30, 21, 10, tzinfo=TZ), 35, 5)
        self.assertEqual(a.decision, "REJECT_ENTRY")

    def test_close_only(self):
        a = assess_late_session(datetime(2026, 10, 30, 21, 30, tzinfo=TZ), 1)
        self.assertEqual(a.decision, "CLOSE_ONLY")

    def test_flatten(self):
        a = assess_late_session(datetime(2026, 10, 30, 21, 45, tzinfo=TZ), 1)
        self.assertEqual(a.decision, "FLATTEN_NOW")

    def test_naive_rejected(self):
        with self.assertRaises(ValueError):
            assess_late_session(datetime(2026, 10, 30, 21, 0), 5)

    def test_invalid_duration_rejected(self):
        with self.assertRaises(ValueError):
            assess_late_session(datetime(2026, 10, 30, 21, 0, tzinfo=TZ), -1)
