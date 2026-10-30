import unittest
import pandas as pd
from market_data.candlesticks import anatomy, describe_patterns


class CandlestickTests(unittest.TestCase):
    def test_geometry(self):
        bars = pd.DataFrame({"Open":[10.0],"High":[14.0],"Low":[8.0],"Close":[13.0]})
        a = anatomy(bars).iloc[0]
        self.assertEqual(a["range"], 6)
        self.assertEqual(a["body"], 3)
        self.assertEqual(a["upper_wick"], 1)
        self.assertEqual(a["lower_wick"], 2)
        self.assertAlmostEqual(a["close_location"], 2/3)

    def test_zero_range_undefined_ratios(self):
        bars = pd.DataFrame({"Open":[10],"High":[10],"Low":[10],"Close":[10]})
        self.assertTrue(pd.isna(anatomy(bars).iloc[0]["body_fraction"]))
        self.assertFalse(describe_patterns(bars).iloc[0]["doji_like"])

    def test_bullish_engulfing(self):
        bars = pd.DataFrame({"Open":[12,9],"High":[13,14],"Low":[9,8],"Close":[10,13]})
        p = describe_patterns(bars)
        self.assertFalse(p.iloc[0]["bullish_body_engulfing"])
        self.assertTrue(p.iloc[1]["bullish_body_engulfing"])

    def test_invalid_bar(self):
        bars = pd.DataFrame({"Open":[10],"High":[9],"Low":[8],"Close":[11]})
        with self.assertRaises(ValueError):
            anatomy(bars)

    def test_inside_bar(self):
        bars = pd.DataFrame({"Open":[10,10],"High":[14,13],"Low":[8,9],"Close":[11,11]})
        self.assertTrue(describe_patterns(bars).iloc[1]["inside_bar"])
