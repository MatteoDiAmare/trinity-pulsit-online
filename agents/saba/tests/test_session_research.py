import unittest
import pandas as pd
from market_data.session_cli import research_snapshot


class SessionResearchTests(unittest.TestCase):
    def test_completed_bar_and_asia_range(self):
        ix = pd.date_range("2026-10-30 00:00", periods=540, freq="min", tz="Europe/London").tz_convert("UTC")
        bars = pd.DataFrame({"High": range(540), "Low": range(540)}, index=ix)
        report = research_snapshot(bars)
        self.assertEqual(report["asia_reference_high"], 479.0)
        self.assertEqual(report["asia_reference_low"], 0.0)
        self.assertTrue(report["as_of_completed_utc"].endswith("+00:00"))

    def test_missing_asia_range_unknown(self):
        ix = pd.date_range("2026-10-30 08:00", periods=30, freq="min", tz="Europe/London").tz_convert("UTC")
        bars = pd.DataFrame({"High": range(30), "Low": range(30)}, index=ix)
        report = research_snapshot(bars)
        self.assertIsNone(report["asia_reference_high"])

    def test_no_entry_after_cutoff(self):
        ix = pd.date_range("2026-10-30 21:29", periods=2, freq="min", tz="Europe/Stockholm").tz_convert("UTC")
        bars = pd.DataFrame({"High": [1, 2], "Low": [0, 1]}, index=ix)
        report = research_snapshot(bars)
        self.assertEqual(report["time_gate"]["decision"], "CLOSE_ONLY")
