import unittest
import pandas as pd
from market_data.market_structure import confirmed_swings, structure_state, prior_range_breaks


class MarketStructureTests(unittest.TestCase):
    def test_confirmation_is_delayed(self):
        bars = pd.DataFrame({"High":[2,3,7,4,3,6,4], "Low":[1,1,2,1,1,2,1]})
        swings = confirmed_swings(bars, left=2, right=2)
        pivot = swings[(swings.kind == "high") & (swings.pivot_at == 2)]
        self.assertEqual(len(pivot), 1)
        self.assertEqual(pivot.iloc[0].confirmed_at, 4)

    def test_equal_highs_are_not_pivots(self):
        bars = pd.DataFrame({"High":[1,3,3,2,1], "Low":[0,0,0,0,0]})
        self.assertFalse((confirmed_swings(bars, left=1, right=1).kind == "high").any())

    def test_no_early_structure(self):
        bars = pd.DataFrame({"High":[1,3,2,4,2,5,2,6], "Low":[0,1,0,2,1,3,2,4]})
        self.assertEqual(structure_state(bars, left=1, right=1).iloc[0]["structure"], "unknown")

    def test_prior_range_excludes_current_bar(self):
        bars = pd.DataFrame({"High":[10,11,20,21], "Low":[8,9,10,11], "Close":[9,10,19,20]})
        result = prior_range_breaks(bars, lookback=2)
        self.assertTrue(result.iloc[2].close_above_prior_range)
        self.assertEqual(result.iloc[2].prior_resistance, 11)
        self.assertFalse(result.iloc[0].close_above_prior_range)

    def test_invalid_lookback(self):
        with self.assertRaises(ValueError):
            prior_range_breaks(pd.DataFrame(), lookback=1)
