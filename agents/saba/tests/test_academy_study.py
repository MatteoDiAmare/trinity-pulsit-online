import json
import math
import tempfile
import unittest
from pathlib import Path
from statistics import NormalDist

try:
    import numpy as np
    import pandas as pd
    from market_data import academy_bridge
    from market_data.laboratory import ExperimentRegistry
    from market_data.archive import MarketArchive
    from market_data.academy import SimConfig, Simulator, build_strategy, load_instrument
    from market_data.academy.forward import ForwardTracker, validate_forecast, verdict_from_trades
    from market_data.academy.integrity import SpecIndex
    from market_data.academy.orchestrator import CycleDenied
    from market_data.academy.stats import bootstrap_mean_ci, deflated_sharpe, expected_max_sharpe, moments, pbo_cscv, sharpe
    from market_data.academy.study import StudySpec, run_recorded_study, run_study, set_path
    from market_data.academy.walkforward import development_end, make_folds
    from tests.test_academy import random_walk
except ImportError:
    pd = None


def bars_from_returns(r, seed_price=100.0, start="2018-01-01"):
    close = seed_price * np.cumprod(1 + r)
    open_ = np.concatenate([[seed_price], close[:-1]])
    idx = pd.bdate_range(start, periods=len(r), tz="UTC")
    df = pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) * 1.002, "Low": np.minimum(open_, close) * 0.998, "Close": close}, index=idx)
    df["Volume"] = 1000.0
    return df


def momentum_bars(n, seed, phi=0.35, vol=0.01):
    rng = np.random.default_rng(seed)
    e, r = rng.normal(0, vol, n), np.zeros(n)
    for t in range(1, n):
        r[t] = phi * r[t - 1] + e[t]
    return bars_from_returns(r)


def noise_bars(n, seed):
    return bars_from_returns(np.random.default_rng(seed).normal(0, 0.01, n))


GRID = {"short": [5, 10, 20], "long": [40, 80]}
MARKETS = ["GOLD", "EURUSD"]


def spec(**kw):
    base = dict(name="t", strategy="sma_cross", base_params={"stop_loss_pct": 2.0}, grid=GRID, instruments=MARKETS, train_bars=300, test_bars=100)
    base.update(kw)
    return StudySpec(**base)


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class StatsTests(unittest.TestCase):
    def test_deflated_sharpe_matches_an_independent_computation(self):
        sr, T, sk, ku, N, V = 0.08, 500, -0.3, 4.5, 20, 0.0004
        euler = 0.5772156649015329
        N_ = NormalDist()
        sr0 = math.sqrt(V) * ((1 - euler) * N_.inv_cdf(1 - 1 / N) + euler * N_.inv_cdf(1 - 1 / (N * math.e)))
        expect = N_.cdf((sr - sr0) * math.sqrt(T - 1) / math.sqrt(1 - sk * sr + (ku - 1) / 4 * sr * sr))
        self.assertAlmostEqual(deflated_sharpe(sr, T, sk, ku, N, V), expect, places=12)

    def test_more_trials_or_noisier_trials_lower_the_deflated_sharpe(self):
        args = (0.08, 500, 0.0, 3.0)
        a, b, c = (deflated_sharpe(*args, n, 0.0004) for n in (1, 10, 100))
        self.assertGreater(a, b)
        self.assertGreater(b, c)
        self.assertGreater(deflated_sharpe(*args, 20, 0.0001), deflated_sharpe(*args, 20, 0.001))
        self.assertGreater(deflated_sharpe(0.08, 2000, 0.0, 3.0, 10, 0.0004), deflated_sharpe(0.08, 200, 0.0, 3.0, 10, 0.0004))
        self.assertEqual(expected_max_sharpe(0.0004, 1), 0.0)
        self.assertLess(expected_max_sharpe(0.0004, 10), expected_max_sharpe(0.0004, 1000))
        self.assertIsNone(deflated_sharpe(0.1, 3, 0.0, 3.0, 5, 0.01))

    def test_single_trial_is_the_probabilistic_sharpe_ratio(self):
        sr, T = 0.1, 400
        self.assertAlmostEqual(deflated_sharpe(sr, T, 0.0, 3.0, 1, 0.0), NormalDist().cdf(sr * math.sqrt(T - 1) / math.sqrt(1 + sr * sr / 2)), places=12)

    def test_pbo_is_about_one_half_for_noise_and_near_zero_with_a_planted_edge(self):
        rng = np.random.default_rng(0)
        noise = rng.normal(0, 0.01, (30, 800))
        p = pbo_cscv(noise)
        self.assertTrue(0.25 <= p["pbo"] <= 0.75, p)
        planted = noise.copy()
        planted[7] += 0.004
        self.assertLess(pbo_cscv(planted)["pbo"], 0.1)
        self.assertEqual(p["combos"], 70)
        self.assertIsNone(pbo_cscv(noise[:1]))
        self.assertIsNone(pbo_cscv(noise[:, :20]))

    def test_helpers(self):
        self.assertEqual(sharpe([1, 1, 1]), 0.0)
        sk, ku = moments(np.random.default_rng(1).normal(size=200000))
        self.assertAlmostEqual(sk, 0, delta=0.05)
        self.assertAlmostEqual(ku, 3, delta=0.1)
        lo, hi = bootstrap_mean_ci(np.random.default_rng(2).normal(2, 1, 300))
        self.assertTrue(lo < 2 < hi)
        self.assertIsNone(bootstrap_mean_ci([1, 2, 3]))


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class FoldTests(unittest.TestCase):
    def test_folds_are_chronological_non_overlapping_and_inside_development_data(self):
        folds = make_folds(1000, 300, 100)
        self.assertEqual(len(folds), 7)
        for a, b in zip(folds, folds[1:]):
            self.assertEqual(b.test_start, a.test_end)
            self.assertEqual(b.train_start, a.train_start + 100)
        for f in folds:
            self.assertLessEqual(f.test_end, 1000)
            self.assertEqual(f.test_start, f.train_end)
            self.assertLess(f.train_start, f.train_end)
        self.assertEqual(folds[-1].test_end, 1000)
        e = make_folds(1000, 300, 100, embargo=10)
        self.assertEqual(e[0].test_start, e[0].train_end + 10)
        self.assertEqual(make_folds(1000, 300, 100, anchored=True)[3].train_start, 0)

    def test_invalid_geometry_is_refused(self):
        for kw in ({"dev_end": 350, "train_bars": 300, "test_bars": 100}, {"dev_end": 1000, "train_bars": 10, "test_bars": 100},
                   {"dev_end": 1000, "train_bars": 300, "test_bars": 100, "step": 50}, {"dev_end": 1000, "train_bars": 300, "test_bars": 100, "embargo": -1}):
            with self.assertRaises(ValueError):
                make_folds(**kw)

    def test_development_end_protects_the_tail_or_follows_the_frontier(self):
        b = noise_bars(1000, 1)
        self.assertEqual(development_end(b), 800)
        self.assertEqual(development_end(b, frontier=b.index[899]), 900)


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class SpecTests(unittest.TestCase):
    def test_grid_limits_and_cells(self):
        s = spec()
        cells = s.cells()
        self.assertEqual(len(cells), 6)
        self.assertEqual(cells, spec().cells())                                     # deterministic order
        self.assertTrue(all(c["stop_loss_pct"] == 2.0 for c in cells))
        with self.assertRaises(ValueError):
            spec(grid={"short": list(range(2, 12)), "long": list(range(20, 30))})   # 100 cells > 60: never truncated silently
        for bad in ({"grid": {}}, {"grid": {"short": []}}, {"grid": {"short": [5, 5]}}, {"instruments": []}, {"name": " "}, {"max_cells": 500}):
            with self.assertRaises(ValueError):
                spec(**bad)

    def test_dotted_paths(self):
        base = {"code": "x", "params": {"fast": 1}}
        self.assertEqual(set_path(base, "params.fast", 9)["params"]["fast"], 9)
        self.assertEqual(base["params"]["fast"], 1)                                   # input untouched
        with self.assertRaises(ValueError):
            set_path(base, "nope.fast", 1)
        with self.assertRaises(ValueError):
            set_path(base, "code.fast", 1)

    def test_hash_changes_with_any_field(self):
        self.assertEqual(spec().sha256, spec().sha256)
        self.assertNotEqual(spec().sha256, spec(train_bars=310).sha256)
        self.assertNotEqual(spec().sha256, spec(grid={"short": [5, 10], "long": [40, 80]}).sha256)


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class StudyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.noise = {m: noise_bars(1100, 10 + k) for k, m in enumerate(MARKETS)}
        cls.mom = {m: momentum_bars(1100, 20 + k, phi=0.4) for k, m in enumerate(MARKETS)}
        cls.dev = {m: 880 for m in MARKETS}
        cls.r_noise = run_study(spec(), cls.noise, cls.dev)
        cls.r_mom = run_study(spec(), cls.mom, cls.dev)

    def test_noise_gives_no_evidence_and_a_low_deflated_sharpe(self):
        r = self.r_noise
        self.assertEqual(r["verdict"], "no_evidence")
        self.assertTrue(r["failed_criteria"])
        self.assertLess(r["wfo"]["dsr"], 0.95)
        self.assertEqual((r["cells"], r["valid_cells"], r["markets"]), (6, 6, MARKETS))
        self.assertEqual(r["folds"], {"GOLD": 5, "EURUSD": 5})

    def test_planted_momentum_is_found_and_beats_noise_on_every_headline_number(self):
        n, m = self.r_noise["wfo"], self.r_mom["wfo"]
        self.assertGreater(m["sharpe_annualized"], 0)
        self.assertGreater(m["sharpe_annualized"], n["sharpe_annualized"])
        self.assertGreater(m["dsr"], n["dsr"])
        self.assertGreater(m["expectancy_per_trade"], n["expectancy_per_trade"])
        self.assertGreater(self.r_mom["benchmarks"]["grid_share_positive_oos"], self.r_noise["benchmarks"]["grid_share_positive_oos"])

    def test_every_criterion_is_reported_and_failures_are_explained(self):
        for r in (self.r_noise, self.r_mom):
            self.assertIn("thresholds", r)
            self.assertEqual(r["verdict"] == "candidate", not r["failed_criteria"])
        self.assertTrue(any("deflated Sharpe" in c for c in self.r_noise["failed_criteria"]))

    def test_nothing_beyond_the_development_end_is_used(self):
        tampered = {m: b.copy() for m, b in self.noise.items()}
        for b in tampered.values():
            b.iloc[880:, :4] *= 40.0
        again = run_study(spec(), tampered, self.dev)
        self.assertEqual(json.dumps(again, sort_keys=True, default=str), json.dumps(self.r_noise, sort_keys=True, default=str))
        for m, txt in again["data_used"].items():
            self.assertIn("bars 0..879", txt)

    def test_parameter_choice_only_sees_the_training_window(self):
        k = 2
        f = make_folds(880, 300, 100)[k]
        tampered = {m: b.copy() for m, b in self.noise.items()}
        for b in tampered.values():
            b.iloc[f.test_start:f.test_end, :4] *= np.random.default_rng(5).uniform(0.5, 2.0, (f.test_end - f.test_start, 4))
            b["High"] = b[["Open", "High", "Low", "Close"]].max(axis=1)
            b["Low"] = b[["Open", "High", "Low", "Close"]].min(axis=1)
        again = run_study(spec(), tampered, self.dev)
        base_row, new_row = self.r_noise["fold_table"][k], again["fold_table"][k]
        self.assertEqual(base_row["selected_cell"], new_row["selected_cell"])        # same choice from the same training data
        self.assertEqual(base_row["train_score"], new_row["train_score"])
        self.assertNotEqual(base_row.get("oos_return_pct"), new_row.get("oos_return_pct"))   # but the unseen window really differs

    def test_reproducible_and_parallel_equals_sequential(self):
        again = run_study(spec(), self.noise, self.dev)
        par = run_study(spec(), self.noise, self.dev, workers=2)
        a = json.dumps(self.r_noise, sort_keys=True, default=str)
        self.assertEqual(a, json.dumps(again, sort_keys=True, default=str))
        self.assertEqual(a, json.dumps(par, sort_keys=True, default=str))

    def test_a_broken_cell_is_excluded_but_still_counted_as_a_trial(self):
        r = run_study(spec(grid={"short": [5, 50], "long": [20, 80]}), self.noise, self.dev)      # short=50 >= long=20 is invalid
        self.assertEqual((r["cells"], r["valid_cells"]), (4, 3))
        bad = [c for c in r["cell_table"] if not c["valid"]]
        self.assertEqual(len(bad), 1)
        self.assertIn("error", bad[0])
        self.assertEqual(r["n_trials_for_deflation"], 4)

    def test_prior_trials_raise_the_hurdle(self):
        low = run_study(spec(), self.mom, self.dev)["wfo"]["dsr"]
        high = run_study(spec(prior_trials=500), self.mom, self.dev)["wfo"]["dsr"]
        self.assertLess(high, low)

    def test_folds_without_an_eligible_parameter_set_stay_flat(self):
        r = run_study(spec(min_train_trades=10 ** 6), self.noise, self.dev)
        self.assertTrue(all(row["selected_cell"] is None for row in r["fold_table"]))
        self.assertEqual(r["wfo"]["trades"], 0)
        self.assertEqual(r["verdict"], "no_evidence")

    def test_sandboxed_strategy_cells_work_with_dotted_grid_paths(self):
        code = ("import numpy as np\nclass Strategy:\n    def __init__(self, params):\n        self.n = params.get('n', 10)\n"
                "    def on_bar(self, view, position):\n        if view.n < self.n + 2:\n            return None\n"
                "        up = view.close[-1] > np.mean(view.close[-self.n:])\n        if position is None:\n"
                "            return Decision('BUY' if up else 'SELL', stop_loss_pct=2.0)\n        return None\n")
        s = StudySpec("code", "sandboxed", {"code": code, "params": {}}, {"params.n": [5, 15]}, ["GOLD"], train_bars=300, test_bars=100)
        r = run_study(s, {"GOLD": self.noise["GOLD"]}, {"GOLD": 700})
        self.assertEqual((r["cells"], r["valid_cells"]), (2, 2))
        self.assertGreater(r["wfo"]["oos_days"], 0)


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class RecordedStudyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.arch = MarketArchive(self.root)
        for iid, seed in (("GOLD", 1), ("EURUSD", 2)):
            sym = load_instrument(iid).symbol
            d = self.root / sym / "1d"
            d.mkdir(parents=True)
            noise_bars(1100, seed).assign(Dividends=0.0, **{"Stock Splits": 0.0}).to_parquet(d / "bars.parquet")
        self.reg = ExperimentRegistry(self.root / "experiments.sqlite3")

    def tearDown(self):
        self.reg.close()
        self.tmp.cleanup()

    def test_recorded_deduplicated_and_counted_as_trials(self):
        a = run_recorded_study(self.reg, self.arch, spec())
        self.assertFalse(a["reused"])
        rows = self.reg.db.execute("SELECT COUNT(*) FROM experiments WHERE name='academy:study'").fetchone()[0]
        self.assertEqual(rows, 1)
        idx = SpecIndex(self.reg)
        for iid in MARKETS:
            self.assertEqual(idx.trials(f"{load_instrument(iid).symbol}|1d"), 6)        # one trial per cell and market
        b = run_recorded_study(self.reg, self.arch, spec())
        self.assertTrue(b["reused"])
        self.assertEqual(self.reg.db.execute("SELECT COUNT(*) FROM experiments WHERE name='academy:study'").fetchone()[0], 1)
        c = run_recorded_study(self.reg, self.arch, spec(name="second", grid={"short": [3, 8], "long": [30, 60]}))
        self.assertEqual(c["prior_trials_counted"], 6)                                    # the 6 earlier cells (same on both markets) raise the hurdle once, not twice
        self.assertEqual(c["n_trials_for_deflation"], 4 + 6)
        self.assertEqual(a["data_used"]["GOLD"].split(" ")[1], "0..879")                  # 80 % of 1100; the last 20 % is reserved

    def test_a_consumed_test_window_moves_the_development_end(self):
        sym = load_instrument("GOLD").symbol
        bars = self.arch.read(sym, "1d")
        self.reg.record("academy:final_test", bars, {"experiment_id": 1, "symbol": sym, "interval": "1d", "test_start_utc": str(bars.index[800]),
                                                      "test_end_utc": str(bars.index[950])}, {})
        r = run_recorded_study(self.reg, self.arch, spec(instruments=["GOLD"]))
        self.assertEqual(r["data_used"]["GOLD"].split(" ")[1], "0..950")                  # consumed bars are development data now; the fresh tail stays out


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.arch = MarketArchive(self.root)
        self.full = momentum_bars(900, 3, phi=0.3)
        self.sym = load_instrument("GOLD").symbol
        self.put(self.full.iloc[:600])
        self.tr = ForwardTracker(self.root / "academy_memory.sqlite3")
        self.forecast = {"p_positive": 0.6, "expected_hit_rate": 0.42, "expected_expectancy": 0.5}
        self.plan = dict(label="sma 5/40", strategy="sma_cross", params={"short": 5, "long": 40, "stop_loss_pct": 2.0}, instruments=["GOLD"], forecast=self.forecast,
                         rules={"min_trades": 10, "min_days": 30})

    def tearDown(self):
        self.tr.close()
        self.tmp.cleanup()

    def put(self, df):
        d = self.root / self.sym / "1d"
        d.mkdir(parents=True, exist_ok=True)
        df.assign(Dividends=0.0, **{"Stock Splits": 0.0}).to_parquet(d / "bars.parquet")

    def test_freeze_requires_a_forecast_and_valid_pre_registered_rules(self):
        for bad in (None, {}, {"p_positive": 1.2, "expected_hit_rate": 0.4, "expected_expectancy": 1}, {"p_positive": 0.5, "expected_hit_rate": 2, "expected_expectancy": 1},
                    {"p_positive": 0.5, "expected_hit_rate": 0.4, "expected_expectancy": float("nan")}):
            with self.assertRaises(ValueError):
                validate_forecast(bad)
        with self.assertRaises(ValueError):
            self.tr.freeze(self.arch, **{**self.plan, "rules": {"min_trades": 3, "min_days": 30}})
        with self.assertRaises(ValueError):
            self.tr.freeze(self.arch, **{**self.plan, "params": {"short": 50, "long": 20}})      # invalid strategy is refused at freezing time
        fid = self.tr.freeze(self.arch, **self.plan)
        c = self.tr.get(fid)
        self.assertEqual(c["cutoffs"]["GOLD"], str(self.full.index[599]).replace(" ", "T") if "T" in c["cutoffs"]["GOLD"] else str(self.full.index[599]))
        self.assertEqual(c["forecast"]["p_positive"], 0.6)
        with self.assertRaises(ValueError):
            self.tr.freeze(self.arch, **self.plan)                                              # identical plan at the same cutoff

    def test_no_new_bars_means_insufficient_data(self):
        self.tr.freeze(self.arch, **self.plan)
        snap = self.tr.update(self.arch)[0]
        self.assertEqual(snap["bars_forward"], 0)
        self.assertEqual(snap["verdict"]["verdict"], "insufficient_data")

    def test_forward_results_use_only_bars_after_the_cutoff_and_are_reproducible(self):
        fid = self.tr.freeze(self.arch, **self.plan)
        self.put(self.full.iloc[:760])
        s1 = self.tr.update(self.arch)[0]
        s2 = self.tr.update(self.arch)[0]
        self.assertEqual(s1["bars_forward"], 160)
        self.assertEqual(s1["pnls"], s2["pnls"])
        direct = Simulator(SimConfig(1000.0, None, None, instrument=load_instrument("GOLD"))).run(
            self.full.iloc[:760], build_strategy("sma_cross", self.plan["params"]), trade_start=600)
        closed = [t["net_pnl"] for t in direct.trades if t["exit_reason"] != "end_of_data"]
        self.assertEqual(s1["pnls"], closed)
        self.assertTrue(all(t["entry_ts"] > self.full.index[599] for t in direct.trades))
        self.assertEqual(len(self.tr.history(fid)), 2)

    def test_earlier_closed_trades_never_change_when_later_bars_arrive(self):
        self.tr.freeze(self.arch, **self.plan)
        self.put(self.full.iloc[:700])
        early = self.tr.update(self.arch)[0]["pnls"]
        self.put(self.full.iloc[:900])
        late = self.tr.update(self.arch)[0]["pnls"]
        self.assertEqual(late[:len(early)], early)
        self.assertGreaterEqual(len(late), len(early))

    def test_verdict_rules(self):
        rules = {"min_trades": 10, "min_days": 30}
        self.assertEqual(verdict_from_trades([1.0] * 5, 100, rules)["verdict"], "insufficient_data")
        self.assertEqual(verdict_from_trades([1.0] * 20, 10, rules)["verdict"], "insufficient_data")
        self.assertEqual(verdict_from_trades([1.0 + 0.1 * (i % 3) for i in range(30)], 100, rules)["verdict"], "confirmed")
        self.assertEqual(verdict_from_trades([-1.0 - 0.1 * (i % 3) for i in range(30)], 100, rules)["verdict"], "refuted")
        self.assertEqual(verdict_from_trades([5.0, -5.0] * 15, 100, rules)["verdict"], "inconclusive")

    def test_forecasts_are_scored_with_the_brier_score(self):
        a = self.tr.freeze(self.arch, **{**self.plan, "forecast": {"p_positive": 0.9, "expected_hit_rate": 0.5, "expected_expectancy": 1.0}})
        b = self.tr.freeze(self.arch, **{**self.plan, "label": "other", "params": {"short": 6, "long": 40, "stop_loss_pct": 2.0},
                                          "forecast": {"p_positive": 0.2, "expected_hit_rate": 0.4, "expected_expectancy": 0.0}})
        c = self.tr.freeze(self.arch, **{**self.plan, "label": "pending", "params": {"short": 7, "long": 40, "stop_loss_pct": 2.0}})
        for fid, exp_, hit, v in ((a, 2.0, 0.55, "confirmed"), (b, -1.0, 0.30, "refuted")):
            self.tr.db.execute("INSERT INTO forward_log(frozen_id,created_at,as_of,snapshot_json) VALUES (?,?,?,?)",
                               (fid, "t", "t", json.dumps({"verdict": {"verdict": v}, "expectancy": exp_, "hit_rate": hit})))
        rep = self.tr.forecast_report()
        self.assertEqual(rep["scored"], 2)
        briers = {r["id"]: r["brier"] for r in rep["candidates"] if "brier" in r}
        self.assertAlmostEqual(briers[a], (0.9 - 1) ** 2)
        self.assertAlmostEqual(briers[b], (0.2 - 0) ** 2)
        self.assertAlmostEqual(rep["mean_brier"], (0.01 + 0.04) / 2)
        self.assertGreater(rep["skill_vs_coin"], 0.8)
        pending = [r for r in rep["candidates"] if r["id"] == c][0]
        self.assertNotIn("brier", pending)                                              # unevaluated candidates are not scored

    def test_retired_candidates_are_not_updated(self):
        fid = self.tr.freeze(self.arch, **self.plan)
        self.tr.retire(fid, "test")
        self.assertEqual(self.tr.update(self.arch), [])


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class BridgeStudyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for iid, seed in (("GOLD", 1), ("EURUSD", 2)):
            d = self.root / load_instrument(iid).symbol / "1d"
            d.mkdir(parents=True)
            noise_bars(1100, seed).assign(Dividends=0.0, **{"Stock Splits": 0.0}).to_parquet(d / "bars.parquet")
        self.sdir = self.root / "strategies"
        (self.sdir / "code").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, req):
        return academy_bridge.handle(req, root=self.root, strategies_dir=self.sdir)

    STUDY = {"name": "bridge study", "strategy": "sma_cross", "base_params": {"stop_loss_pct": 2.0}, "grid": {"short": [5, 10], "long": [40, 80]},
             "instruments": ["GOLD", "EURUSD"], "train_bars": 300, "test_bars": 100}

    def test_study_freeze_and_report_through_the_bridge(self):
        cyc = self.call({"command": "start_cycle"})["cycle_id"]
        out = self.call({"command": "run_study", "cycle_id": cyc, "study": self.STUDY})
        self.assertEqual(out["cells"], 4)
        self.assertIn(out["verdict"], ("candidate", "no_evidence"))
        self.assertLessEqual(len(out["cell_table"]), 8)
        again = self.call({"command": "run_study", "cycle_id": cyc, "study": self.STUDY})
        self.assertTrue(again["reused"])
        fz = self.call({"command": "freeze_candidate", "label": "sma", "strategy": "sma_cross", "params": {"short": 10, "long": 40, "stop_loss_pct": 2.0},
                        "instruments": ["GOLD"], "forecast": {"p_positive": 0.4, "expected_hit_rate": 0.4, "expected_expectancy": 0.2},
                        "study_sha256": out["study_sha256"]})
        self.assertEqual(fz["frozen_id"], 1)
        rep = self.call({"command": "forward_report"})
        self.assertEqual(rep["snapshots"][0]["verdict"]["verdict"], "insufficient_data")
        self.assertEqual(rep["forecast_report"]["scored"], 0)
        with self.assertRaises(ValueError):
            self.call({"command": "freeze_candidate", "label": "x", "strategy": "sma_cross", "params": {}, "instruments": ["GOLD"]})   # no forecast

    def test_a_study_is_charged_to_the_cycle_budget_and_obeys_halt(self):
        cyc = self.call({"command": "start_cycle"})["cycle_id"]
        for k in range(20):                                    # the default budget is 20 experiments per cycle; each study costs one
            self.call({"command": "run_study", "cycle_id": cyc, "study": {**self.STUDY, "name": f"s{k}", "grid": {"short": [5 + k, 30 + k], "long": [90 + k]}}})
        with self.assertRaises(CycleDenied) as cm:
            self.call({"command": "run_study", "cycle_id": cyc, "study": {**self.STUDY, "name": "one too many", "grid": {"short": [3], "long": [33]}}})
        self.assertIn("experiments left", str(cm.exception))
        self.call({"command": "halt", "reason": "test"})
        cyc2 = None
        with self.assertRaises(CycleDenied):
            self.call({"command": "run_study", "cycle_id": cyc, "study": self.STUDY})

    def test_own_code_file_can_be_studied_and_bad_input_is_refused(self):
        (self.sdir / "code" / "mom.py").write_text("import numpy as np\nclass Strategy:\n    def __init__(self, params):\n        self.n = params.get('n', 10)\n"
                                                    "    def on_bar(self, view, position):\n        if view.n < self.n + 2 or position is not None:\n            return None\n"
                                                    "        return Decision('BUY' if view.close[-1] > np.mean(view.close[-self.n:]) else 'SELL', stop_loss_pct=2.0)\n")
        cyc = self.call({"command": "start_cycle"})["cycle_id"]
        out = self.call({"command": "run_study", "cycle_id": cyc, "study": {"name": "code", "code_file": "mom.py", "base_params": {"n": 10}, "grid": {"params.n": [5, 20]},
                                                                           "instruments": ["GOLD"], "train_bars": 300, "test_bars": 100}})
        self.assertEqual(out["valid_cells"], 2)
        for bad in ({"study": None}, {"study": {**self.STUDY, "grid": {}}}, {"study": {**self.STUDY, "instruments": ["DOGE"]}}):
            with self.assertRaises(Exception):
                self.call({"command": "run_study", "cycle_id": cyc, **bad})


if __name__ == "__main__":
    unittest.main()
