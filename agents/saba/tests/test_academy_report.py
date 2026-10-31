import json
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
    from market_data.laboratory import ExperimentRegistry
    from market_data.academy import SimConfig, load_instrument
    from market_data.academy.experiments import Split, run_training
    from market_data.academy.ledger import LedgerStore
    from market_data.academy.memory import ResearchMemory
    from market_data.academy.report import bootstrap_ci, buy_and_hold, build_report, render_markdown, trade_stats
    from tests.test_academy import random_walk
except ImportError:
    pd = None

PLAN = [{"strategy": "sma_cross", "params": {"short": 5, "long": 20}, "label": "sma"},
        {"strategy": "breakout", "params": {"window": 15}, "label": "brk"},
        {"strategy": "mean_reversion", "params": {"window": 20, "z_entry": 1.5}, "label": "mr"}]


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        d = self.root / "GC=F" / "1d"
        d.mkdir(parents=True)
        self.bars = random_walk(900, seed=5)
        self.bars.assign(Dividends=0.0, **{"Stock Splits": 0.0}).to_parquet(d / "bars.parquet")
        self.reg = ExperimentRegistry(self.root / "experiments.sqlite3")
        self.store = LedgerStore(self.root)
        self.mem = ResearchMemory(self.root / "academy_memory.sqlite3")
        self.cfg = SimConfig(1000.0, instrument=load_instrument("GOLD"), max_exposure=None)
        run_training(self.bars, self.reg, symbol="GC=F", interval="1d", plan=PLAN[:1], sim_config=self.cfg, control_runs=0, ledger=self.store,
                     split=Split.chronological(len(self.bars)))
        self.first_id = self.reg.db.execute("SELECT MAX(id) FROM experiments WHERE name LIKE 'academy:sma%'").fetchone()[0]
        run_training(self.bars, self.reg, symbol="GC=F", interval="1d", plan=PLAN, sim_config=self.cfg, control_runs=0, ledger=self.store,
                     split=Split.chronological(len(self.bars)))
        self.zero = {"max_registry_id": 0, "max_lesson_id": 0, "max_hyp_id": 0, "max_cycle_id": 0}

    def tearDown(self):
        self.mem.close()
        self.reg.close()
        self.tmp.cleanup()

    def test_trade_stats_known_values(self):
        t = [{"net_pnl": 10.0, "net_return_pct": 1.0, "fees": 0.5, "spread_slippage_cost": 0.5, "r_multiple": 1.0},
             {"net_pnl": -4.0, "net_return_pct": -0.4, "fees": 0.5, "spread_slippage_cost": 0.5, "r_multiple": -0.4},
             {"net_pnl": -6.0, "net_return_pct": -0.6, "fees": 0.5, "spread_slippage_cost": 0.5, "r_multiple": -0.6}]
        s = trade_stats(t)
        self.assertEqual((s["trades"], s["wins"], s["losses"]), (3, 1, 2))
        self.assertAlmostEqual(s["hit_rate"], 1 / 3)
        self.assertAlmostEqual(s["avg_win"], 10.0)
        self.assertAlmostEqual(s["avg_loss"], -5.0)
        self.assertAlmostEqual(s["payoff_ratio"], 2.0)
        self.assertAlmostEqual(s["expectancy"], 0.0)
        self.assertAlmostEqual(s["avg_cost"], 1.0)
        self.assertEqual(trade_stats([])["trades"], 0)
        self.assertIsNone(bootstrap_ci([1.0, 2.0]))
        lo, hi = bootstrap_ci(list(np.random.default_rng(0).normal(5, 1, 200)))
        self.assertTrue(lo < 5 < hi)

    def test_counts_and_pools_match_a_direct_read_of_the_ledgers(self):
        rep = build_report(self.root, self.zero, random_runs=3)
        ids = self.store.experiments()
        direct = {p: 0 for p in ("train", "validation", "final_test")}
        wins = {p: 0 for p in direct}
        for i in ids:
            for p in self.store.periods(i):
                tr = self.store.read_period(i, p)["trades"]
                direct[p] += len(tr)
                wins[p] += sum(t["net_pnl"] > 0 for t in tr)
        self.assertEqual(rep["experiments"], len(ids))
        for p in direct:
            self.assertEqual(rep["by_period"][p]["trades"], direct[p])
            self.assertEqual(rep["by_period"][p]["wins"], wins[p])
        self.assertEqual(rep["overall"]["trades"], sum(direct.values()))
        self.assertEqual(sum(r["trades"] for r in rep["rows"]), sum(direct.values()))
        self.assertEqual(set(rep["experiments_by_label"]), {"sma", "brk", "mr"})

    def test_baseline_excludes_earlier_experiments(self):
        rep = build_report(self.root, {**self.zero, "max_registry_id": self.first_id}, random_runs=0)
        self.assertEqual(rep["experiments"], 2)                     # sma was deduplicated/reused, brk and mr are new
        self.assertNotIn("sma", rep["experiments_by_label"])
        self.assertGreater(rep["comparison_to_pre_session"]["pre_session_validation"]["trades"], 0)
        self.assertIn("hit_rate_p_two_sided", rep["comparison_to_pre_session"])

    def test_benchmarks_are_computed_from_the_same_bars(self):
        rep = build_report(self.root, self.zero, random_runs=4)
        row = next(r for r in rep["rows"] if r["period"] == "validation")
        bpy = load_instrument("GOLD").bars_per_year("1d")
        bh = buy_and_hold(self.bars, pd.Timestamp(row["start"], tz="UTC"), pd.Timestamp(row["end"], tz="UTC"), bpy)
        self.assertAlmostEqual(row["bh_return_pct"], bh["return_pct"], places=6)
        seg = self.bars.loc[row["start"]:row["end"], "Close"]
        self.assertAlmostEqual(row["bh_return_pct"], (seg.iloc[-1] / seg.iloc[0] - 1) * 100, places=6)
        self.assertEqual(row["null"]["runs"], 4)
        self.assertIsNotNone(row["luck_benchmark_sharpe"])
        again = build_report(self.root, self.zero, random_runs=4)
        self.assertEqual(again["rows"][0]["null"], rep["rows"][0]["null"])        # the random null is seeded and reproducible

    def test_lessons_citations_and_markdown(self):
        hid = self.mem.register_hypothesis("Builds on L1: breakout after a refuted SMA", "trend", 4,
                                           {"experiments": [{"strategy": "breakout", "params": {}}], "instruments": ["GOLD"]})
        eid = self.reg.db.execute("SELECT MIN(id) FROM experiments WHERE name LIKE 'academy:%'").fetchone()[0]
        self.mem.add_lesson(self.reg, claim="SMA has no durable edge", verdict="refuted", confidence=0.7, evidence=[{"experiment_id": eid}],
                            scope={"market": "GOLD"}, hypothesis_id=hid)
        self.mem.add_lesson(self.reg, claim="Because of L1 I dropped trend filters", verdict="inconclusive", confidence=0.3,
                            evidence=[{"experiment_id": eid}], scope={"market": "GOLD"})
        rep = build_report(self.root, self.zero, random_runs=0, transcript_text="Ignore this line\nPrior lesson L1 says SMA is a dead end, so I pivot to volume.")
        self.assertEqual(len(rep["lessons"]), 2)
        who = {(c.get("hypothesis"), c.get("lesson")): c["cites_lessons"] for c in rep["citations"]}
        self.assertEqual(who[(hid, None)], [1])
        self.assertEqual(who[(None, 2)], [1])
        self.assertEqual(len(rep["narration_excerpts"]), 1)
        md = render_markdown(rep)
        for heading in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5.", "## 6."):
            self.assertIn(heading, md)
        json.dumps(rep, default=str)                               # serialisable

    def test_empty_session_is_reported_without_errors(self):
        far = {**self.zero, "max_registry_id": 10 ** 6}
        rep = build_report(self.root, far, random_runs=0)
        self.assertEqual(rep["experiments"], 0)
        self.assertIn("Saba session report", render_markdown(rep))


if __name__ == "__main__":
    unittest.main()
