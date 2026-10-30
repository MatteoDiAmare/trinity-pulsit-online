import unittest
import numpy as np
import pandas as pd
from market_data.intelligence import resample_completed, analyze_frame


class IntelligenceTests(unittest.TestCase):
    def setUp(self):
        idx = pd.date_range("2026-10-26 09:00", periods=120, freq="min", tz="UTC")
        price = 2000 + np.sin(np.arange(120) / 4) * 3 + np.arange(120) * .05
        self.bars = pd.DataFrame({
            "Open": price, "High": price + 1, "Low": price - 1,
            "Close": price + .2, "Volume": np.ones(120) * 10,
        }, index=idx)

    def test_complete_buckets_only(self):
        result = resample_completed(self.bars, 5)
        self.assertEqual(len(result), 24)
        self.assertEqual(result.index[0], pd.Timestamp("2026-10-26 09:05", tz="UTC"))

    def test_partial_bucket_excluded(self):
        result = resample_completed(self.bars.iloc[:-1], 5)
        self.assertEqual(len(result), 23)

    def test_gap_excluded(self):
        result = resample_completed(self.bars.drop(self.bars.index[4]), 5)
        self.assertEqual(len(result), 23)

    def test_analysis_unknown_execution_inputs(self):
        result = analyze_frame(self.bars)
        self.assertEqual(result["bars"], 120)
        self.assertTrue(result["assessments"])
        for assessment in result["assessments"].values():
            self.assertIn("spread", assessment["missing_context"])
            self.assertIn("event_calendar", assessment["missing_context"])
            self.assertNotEqual(assessment["status"], "supported")

    def test_causality(self):
        before = analyze_frame(self.bars.iloc[:100])
        after = analyze_frame(self.bars)
        self.assertEqual(before["completed_at_utc"], self.bars.index[99].isoformat())
        self.assertNotEqual(before["completed_at_utc"], after["completed_at_utc"])
