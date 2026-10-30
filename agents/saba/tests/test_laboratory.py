import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
    from market_data.laboratory import evaluate, moving_average_signal, source_ablation, ExperimentRegistry
except ImportError:
    pd = None


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class LaboratoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        index = pd.date_range("2025-01-01", periods=80, tz="UTC", freq="D")
        values = np.linspace(100, 130, 80)
        self.bars = pd.DataFrame({"Open": values, "Close": values}, index=index)

    def tearDown(self):
        self.tmp.cleanup()

    def test_causal_signal_and_holdout(self):
        signal = moving_average_signal(self.bars, 5, 20)
        self.assertTrue((signal.signal.iloc[:19] == 0).all())
        result = evaluate(self.bars, signal)
        self.assertGreater(result["train"]["bars"], 0)
        self.assertGreater(result["holdout"]["bars"], 0)

    def test_costs_reduce_returns(self):
        signal = moving_average_signal(self.bars, 5, 20)
        free = evaluate(self.bars, signal, fee_bps=0, spread_bps=0, slippage_bps=0)
        costly = evaluate(self.bars, signal, fee_bps=20, spread_bps=20, slippage_bps=20)
        self.assertGreaterEqual(free["holdout"]["net_return_pct"], costly["holdout"]["net_return_pct"])

    def test_ablation_and_registry(self):
        signals = moving_average_signal(self.bars, 5, 20)
        result = source_ablation(self.bars, {"baseline": signals, "flat": signals.assign(signal=0)})
        self.assertEqual(set(result), {"baseline", "flat"})
        store = ExperimentRegistry(Path(self.tmp.name) / "experiments.db")
        i = store.record("baseline", self.bars, {"window": 20}, result)
        self.assertEqual(store.recent()[0]["id"], i)
        store.close()

    def test_bad_signal_rejected(self):
        signals = moving_average_signal(self.bars, 5, 20)
        signals.iloc[-1, 0] = 2
        with self.assertRaises(ValueError):
            evaluate(self.bars, signals)
