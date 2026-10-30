import tempfile
import unittest
from pathlib import Path

try:
    import pandas as pd
    from market_data.archive import MarketArchive, normalize_frame
    from market_data.research import describe
except ImportError:
    pd = None


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class MarketDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = MarketArchive(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def bars(self, count=3):
        index = pd.date_range("2026-01-01", periods=count, freq="D", tz="UTC")
        return pd.DataFrame({"Open": [100., 102., 104.][:count], "High": [103., 105., 107.][:count], "Low": [99., 101., 103.][:count], "Close": [102., 104., 106.][:count], "Volume": [10., 11., 12.][:count]}, index=index)

    def test_ingest_and_overlap_deduplication(self):
        data = self.bars()
        class Ticker:
            def history(self, **kwargs):
                return data
        def provider(symbol):
            self.assertEqual(symbol, "GC=F")
            return Ticker()
        first = self.archive.ingest("GC=F", provider=provider)
        second = self.archive.ingest("GC=F", provider=provider)
        self.assertEqual(first["row_count"], 3)
        self.assertEqual(second["row_count"], 3)
        self.assertEqual(len(self.archive.read("GC=F")), 3)
        self.assertTrue((Path(self.temp.name) / "GC=F/1d/metadata.json").exists())
        self.assertEqual(describe("GC=F", root=self.temp.name)["observations"], 3)

    def test_reject_path_traversal_and_unknown_interval(self):
        for symbol, interval in [("../secret", "1d"), ("GC=F", "invalid")]:
            with self.assertRaises(ValueError):
                self.archive.info(symbol, interval)

    def test_invalid_bars_rejected(self):
        frame = self.bars()
        frame.loc[frame.index[0], "High"] = 10
        cleaned = normalize_frame(frame)
        self.assertEqual(len(cleaned), 2)

    def test_date_filter(self):
        frame = self.bars()
        class Ticker:
            def history(self, **kwargs):
                return frame
        self.archive.ingest("GC=F", provider=lambda symbol: Ticker())
        self.assertEqual(len(self.archive.read("GC=F", start="2026-01-02", end="2026-01-02")), 1)
