import unittest
from market_data.market_context import assess_context


class MarketContextTests(unittest.TestCase):
    def test_supported_only_with_explicit_context(self):
        r = assess_context(direction="bullish", structure="bullish",
                           at_known_support=True, volatility_ratio=1.2,
                           spread_bps=2.0, event_risk=False)
        self.assertEqual(r["status"], "supported")

    def test_same_pattern_different_structure(self):
        common = dict(direction="bullish", at_known_support=True,
                      volatility_ratio=1.2, spread_bps=2.0, event_risk=False)
        self.assertEqual(assess_context(structure="bullish", **common)["status"], "supported")
        self.assertEqual(assess_context(structure="bearish", **common)["status"], "contradicted")

    def test_missing_event_calendar_is_not_safe(self):
        r = assess_context(direction="bullish", structure="bullish",
                           volatility_ratio=1, spread_bps=2)
        self.assertEqual(r["status"], "insufficient_evidence")
        self.assertIn("event_calendar", r["missing_context"])

    def test_high_spread_conflicts(self):
        r = assess_context(direction="bullish", structure="bullish",
                           volatility_ratio=1, spread_bps=12, event_risk=False)
        self.assertEqual(r["status"], "contradicted")

    def test_invalid_data_conflicts(self):
        r = assess_context(direction="bearish", structure="bearish",
                           volatility_ratio=1, spread_bps=2, event_risk=False,
                           data_valid=False)
        self.assertEqual(r["status"], "contradicted")

    def test_unknown_structure(self):
        r = assess_context(direction="bullish", structure="unknown",
                           volatility_ratio=1, spread_bps=2, event_risk=False)
        self.assertEqual(r["status"], "insufficient_evidence")

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            assess_context(direction="bullish", structure="bullish",
                           volatility_ratio=float("nan"))
