import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from market_data.trinity_bridge import handle


SNAPSHOT = {"timeframes": {"1m": {
    "completed_at_utc": "2026-10-30T13:00:00+00:00",
    "structure": "unknown", "last_close": 2700.0,
    "uncertainties": ["no executable spread"]}}}


class TrinityBridgeTests(unittest.TestCase):
    def test_analyze_returns_archive_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("market_data.trinity_bridge.analyze_archive", return_value=SNAPSHOT):
                self.assertEqual(handle({"command": "analyze"}, journal_path=Path(folder)/"j.db"), SNAPSHOT)

    def test_decision_is_audited_without_order(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("market_data.trinity_bridge.analyze_archive", return_value=SNAPSHOT):
                result = handle({
                    "command": "decide", "side": "ABSTAIN",
                    "evidence_completed_at_utc": "2026-10-30T13:00:00+00:00",
                    "rationale": "No verified spread",
                    "expected_holding_minutes": 20, "confidence": 0.3,
                }, journal_path=Path(folder)/"j.db")
            self.assertEqual(result["execution"], "DISABLED")
            self.assertEqual(result["decision"], "ABSTAIN")
            self.assertTrue(result["journal_sha256"])
            self.assertTrue((Path(folder)/"j.db").exists())

    def test_rejects_invented_timestamp(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("market_data.trinity_bridge.analyze_archive", return_value=SNAPSHOT):
                with self.assertRaisesRegex(ValueError, "stale"):
                    handle({
                        "command": "decide", "side": "LONG",
                        "evidence_completed_at_utc": "2026-10-30T14:00:00+00:00",
                        "rationale": "Breakout", "invalidation_price": 2690,
                        "expected_holding_minutes": 20, "confidence": 0.6,
                    }, journal_path=Path(folder)/"j.db")

    def test_rejects_other_instrument(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                handle({"command": "analyze", "symbol": "XAUUSD"}, journal_path=Path(folder)/"j.db")
