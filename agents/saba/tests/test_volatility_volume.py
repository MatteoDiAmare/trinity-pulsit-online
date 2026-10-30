import unittest
import numpy as np
import pandas as pd
from market_data.volatility_volume import volatility_volume_features


class VolatilityVolumeTests(unittest.TestCase):
    def setUp(self):
        self.bars = pd.DataFrame({
            "Open": [100., 101., 102., 103., 104., 105.],
            "High": [102., 103., 104., 105., 106., 110.],
            "Low": [99., 100., 101., 102., 103., 104.],
            "Close": [101., 102., 103., 104., 105., 109.],
            "Volume": [10., 20., 10., 20., 10., 100.],
        })

    def test_current_bar_not_in_baseline(self):
        a = volatility_volume_features(self.bars, window=3)
        self.assertAlmostEqual(a.iloc[5]["prior_mean_volume"], (10 + 20 + 10) / 3)
        self.assertAlmostEqual(a.iloc[5]["relative_volume"], 100 / ((10 + 20 + 10) / 3))

    def test_warmup_not_invented(self):
        a = volatility_volume_features(self.bars, window=3)
        self.assertTrue(np.isnan(a.iloc[0]["prior_atr"]))
        self.assertTrue(np.isnan(a.iloc[0]["relative_volume"]))

    def test_missing_volume_stays_unknown(self):
        b = self.bars.copy()
        b.loc[3, "Volume"] = np.nan
        a = volatility_volume_features(b, window=3)
        self.assertTrue(np.isnan(a.iloc[3]["volume"]))
        self.assertTrue(np.isnan(a.iloc[5]["relative_volume"]))

    def test_no_volume_column(self):
        a = volatility_volume_features(self.bars.drop(columns="Volume"), window=3)
        self.assertNotIn("relative_volume", a.columns)

    def test_invalid_volume(self):
        b = self.bars.copy()
        b.loc[2, "Volume"] = -1
        with self.assertRaises(ValueError):
            volatility_volume_features(b)

    def test_no_future_leakage(self):
        before = volatility_volume_features(self.bars.iloc[:5], window=3)
        after = volatility_volume_features(self.bars, window=3)
        pd.testing.assert_frame_equal(before, after.iloc[:5])
