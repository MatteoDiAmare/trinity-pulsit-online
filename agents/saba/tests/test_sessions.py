import unittest
import pandas as pd
from market_data.sessions import session_labels, prior_asia_range


class SessionTests(unittest.TestCase):
    def test_london_summer_winter_dst(self):
        ix = pd.DatetimeIndex(["2026-07-01 07:00Z", "2026-12-01 08:00Z"])
        self.assertEqual(session_labels(ix)["london_open_window"].tolist(), [True, True])

    def test_us_uk_transition_differ(self):
        ix = pd.DatetimeIndex(["2026-03-20 13:30Z", "2026-04-03 13:30Z"])
        labels = session_labels(ix)
        self.assertEqual(labels["ny_cash_open_window"].tolist(), [True, True])
        self.assertEqual(labels["london_open_window"].tolist(), [False, False])

    def test_asia_range_available_only_after_open(self):
        ix = pd.date_range("2026-10-30 00:00", periods=540, freq="min", tz="Europe/London").tz_convert("UTC")
        bars = pd.DataFrame({"High": range(540), "Low": range(540)}, index=ix)
        out = prior_asia_range(bars)
        self.assertTrue(pd.isna(out.iloc[479]["asia_high"]))
        self.assertEqual(out.iloc[480]["asia_high"], 479)
        self.assertEqual(out.iloc[480]["asia_low"], 0)

    def test_missing_minute_disables_range(self):
        ix = pd.date_range("2026-10-30 00:00", periods=540, freq="min", tz="Europe/London").tz_convert("UTC")
        ix = ix.delete(100)
        bars = pd.DataFrame({"High": range(len(ix)), "Low": range(len(ix))}, index=ix)
        self.assertTrue(prior_asia_range(bars)["asia_high"].isna().all())

    def test_naive_index_rejected(self):
        with self.assertRaises(ValueError):
            session_labels(pd.date_range("2026-10-30", periods=2))
