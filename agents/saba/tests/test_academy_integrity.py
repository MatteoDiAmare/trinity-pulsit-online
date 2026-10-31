import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
    from market_data.laboratory import ExperimentRegistry
    from market_data.academy import experiments as ex
    from market_data.academy.experiments import Split, run_final_test, run_training
    from market_data.academy.integrity import (FINAL_TESTS_PER_WINDOW, SpecIndex, expected_max_noise_sharpe,
                                               final_tests, test_frontier, window_id, code_version, spec_hash)
    from market_data.academy.simulator import SimConfig
    from tests.test_academy import random_walk
except ImportError:
    pd = None


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = ExperimentRegistry(Path(self.tmp.name) / "r.db")
        self.all_bars = random_walk(1000, seed=9)
        self.bars = self.all_bars.iloc[:700]

    def tearDown(self):
        self.reg.close()
        self.tmp.cleanup()

    def train(self, bars=None, seed=7, n=30, **kw):
        return run_training(self.bars if bars is None else bars, self.reg, symbol="TEST", interval="1d",
                            experiments=n, seed=seed, control_runs=2, **kw)

    def exp_ids(self):
        return [r[0] for r in self.reg.db.execute("SELECT id FROM experiments WHERE name LIKE 'academy:%' AND name NOT IN "
                                                  "('academy:train_summary','academy:final_test') ORDER BY id")]

    # ---- final-test protection ----
    def test_prefix_consistency_of_fixture(self):
        pd.testing.assert_frame_equal(random_walk(1000, seed=9).iloc[:700], random_walk(700, seed=9))

    def test_no_override_and_no_repeat(self):
        self.train()
        eid = self.exp_ids()[0]
        with self.assertRaises(TypeError):
            run_final_test(self.bars, self.reg, eid, force=True)
        run_final_test(self.bars, self.reg, eid)
        with self.assertRaises(ValueError) as cm:
            run_final_test(self.bars, self.reg, eid)
        self.assertIn("cannot be repeated", str(cm.exception))

    def test_window_budget_exhausts(self):
        self.train()
        ids = self.exp_ids()
        for eid in ids[:FINAL_TESTS_PER_WINDOW]:
            out = run_final_test(self.bars, self.reg, eid)
            self.assertEqual(out["window_budget"], FINAL_TESTS_PER_WINDOW)
        with self.assertRaises(ValueError) as cm:
            run_final_test(self.bars, self.reg, ids[FINAL_TESTS_PER_WINDOW])
        self.assertIn("exhausted", str(cm.exception))

    def test_frontier_blocks_training_until_fresh_data_then_moves_test_window(self):
        self.train()
        run_final_test(self.bars, self.reg, self.exp_ids()[0])
        frontier = test_frontier(self.reg, "TEST", "1d")
        self.assertEqual(frontier, self.bars.index[-1])
        with self.assertRaises(ValueError) as cm:                         # no new data yet
            self.train()
        self.assertIn("no fresh final-test window", str(cm.exception))
        with self.assertRaises(ValueError):
            self.train(self.all_bars.iloc[:715])                          # only 15 fresh bars
        out = self.train(self.all_bars.iloc[:900], seed=8)                # 200 fresh bars
        self.assertEqual(out["split"]["val_end_index"], 700)              # old test window is development data now
        self.assertEqual(out["split"]["test_start_utc"], self.all_bars.index[700].isoformat())
        sel = out["selected_experiment_id"]
        if sel is not None:
            res = run_final_test(self.all_bars.iloc[:900], self.reg, sel)
            self.assertEqual(res["final_test"]["bars"], 200)
            self.assertEqual(test_frontier(self.reg, "TEST", "1d"), self.all_bars.index[899])

    def test_overlapping_window_from_older_training_is_refused(self):
        a = self.train(self.all_bars.iloc[:700], seed=1)
        b = self.train(self.all_bars.iloc[:800], seed=2)
        ea = [r[0] for r in self.reg.db.execute("SELECT id FROM experiments WHERE configuration_json LIKE '%\"val_end_index\": 560%' AND name LIKE 'academy:%' AND name NOT LIKE '%summary%'")]
        eb = [r[0] for r in self.reg.db.execute("SELECT id FROM experiments WHERE configuration_json LIKE '%\"val_end_index\": 640%' AND name LIKE 'academy:%' AND name NOT LIKE '%summary%'")]
        self.assertTrue(ea and eb)
        run_final_test(self.all_bars.iloc[:800], self.reg, ea[0])
        with self.assertRaises(ValueError) as cm:
            run_final_test(self.all_bars.iloc[:800], self.reg, eb[0])
        self.assertIn("overlaps", str(cm.exception))

    def test_too_few_test_bars_refused(self):
        self.train()
        eid = self.exp_ids()[0]
        with self.assertRaises(ValueError):
            run_final_test(self.bars.iloc[:575], self.reg, eid)

    def test_legacy_final_test_record_without_symbol_still_counts(self):
        self.train()
        eid = self.exp_ids()[0]
        self.reg.record("academy:final_test", self.bars.iloc[560:], {"experiment_id": eid, "test_start_utc": self.bars.index[560].isoformat(),
                        "test_end_utc": self.bars.index[-1].isoformat()}, {})
        self.assertEqual(test_frontier(self.reg, "TEST", "1d"), self.bars.index[-1])
        self.assertEqual(len(final_tests(self.reg, "TEST", "1d")), 1)

    # ---- spec hash / dedupe / trials ----
    def test_identical_experiments_are_reused_not_rerun(self):
        a = self.train()
        rows_before = self.reg.db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
        b = self.train()
        self.assertEqual((b["new_trials"], b["reused_experiments"]), (0, 30))
        self.assertEqual(self.reg.db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0], rows_before + 1)   # only a summary
        self.assertEqual(a["selected_experiment_id"], b["selected_experiment_id"])
        self.assertEqual(a["trials_total_on_market"], b["trials_total_on_market"])

    def test_config_or_data_change_is_a_new_experiment(self):
        self.train()
        from market_data.academy import StressModel
        b = self.train(sim_config=SimConfig(1000, stress=StressModel(spread_multiplier=2)))
        self.assertEqual(b["reused_experiments"], 0)
        c = self.train(self.all_bars.iloc[:710])      # different dev data
        self.assertEqual(c["reused_experiments"], 0)

    def test_code_version_change_invalidates_reuse_and_is_recorded(self):
        self.train()
        original = ex.code_version
        ex.code_version = lambda: "deadbeef+dirty"
        try:
            b = self.train()
        finally:
            ex.code_version = original
        self.assertEqual(b["reused_experiments"], 0)
        self.assertEqual(b["code_version"], "deadbeef+dirty")
        cfg = self.reg.recent(1000)
        self.assertTrue(any(r["configuration"].get("code_version") == "deadbeef+dirty" for r in cfg))
        self.assertTrue(all("spec_sha256" in r["configuration"] for r in cfg if r["name"].startswith("academy:") and "params" in r["configuration"]))

    def test_trials_accumulate_and_noise_benchmark_grows(self):
        a = self.train(seed=1, n=20)
        b = self.train(seed=2, n=20)
        self.assertEqual(a["trials_total_on_market"], 20)
        self.assertEqual(b["trials_total_on_market"], 20 + b["new_trials"])
        self.assertGreater(b["noise_benchmark_sharpe"], a["noise_benchmark_sharpe"])
        self.assertIn(b["selected_exceeds_noise_benchmark"], (True, False, None))

    def test_noise_benchmark_function(self):
        f = expected_max_noise_sharpe
        self.assertLess(f(10, 500, 252), f(1000, 500, 252))
        self.assertLess(f(100, 2000, 252), f(100, 200, 252))
        self.assertEqual(f(1, 500, 252), f(2, 500, 252))
        self.assertEqual(f(5, 1, 252), float("inf"))

    def test_spec_hash_sensitivity_and_stability(self):
        base = ("sma_cross", {"short": 5, "long": 20}, {"capital": 1000}, "abc", {"train_end": 10}, "v1")
        self.assertEqual(spec_hash(*base), spec_hash(*base))
        for i, other in enumerate(({"short": 6, "long": 20}, {"capital": 999}, "abd", {"train_end": 11}, "v2")):
            args = list(base)
            args[i + 1] = other
            self.assertNotEqual(spec_hash(*base), spec_hash(*args))

    def test_window_id_is_stable_and_market_specific(self):
        self.assertEqual(window_id("A", "1d", "2025-01-01"), window_id("A", "1d", "2025-01-01"))
        self.assertNotEqual(window_id("A", "1d", "2025-01-01"), window_id("B", "1d", "2025-01-01"))

    def test_code_version_reports_something(self):
        self.assertTrue(isinstance(code_version(), str) and code_version())


if __name__ == "__main__":
    unittest.main()
