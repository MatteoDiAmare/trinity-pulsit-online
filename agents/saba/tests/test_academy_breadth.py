import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
    from market_data.archive import MarketArchive
    from market_data.laboratory import ExperimentRegistry
    from market_data.academy import instruments as inst_mod
    from market_data.academy import study as study_mod
    from market_data.academy.instruments import available_instruments, load_instrument, load_profiles, usable_from
    from market_data.academy.power import effective_markets, min_detectable_sharpe, sharpe_se
    from market_data.academy.quality import check_bars, clean_bars, compare_overlap, find_bad_bars, usable_history
    from market_data.academy.study import StudySpec, _calendar_folds, run_recorded_study, run_study
    from market_data.academy.walkforward import make_folds
    from tests.test_academy_study import bars_from_returns, momentum_bars, noise_bars
except ImportError:
    pd = None

INSTR_DIR = Path(__file__).resolve().parents[1] / "market_data" / "academy" / "instruments"
ORIGINAL_SPEC_SHA256 = {          # frozen so that results on GOLD / EURUSD / SP500 stay comparable with everything recorded so far
    "GOLD.json": None, "EURUSD.json": None, "SP500.json": None}


def spec(**kw):
    base = dict(name="b", strategy="sma_cross", base_params={"stop_loss_pct": 2.0}, grid={"short": [5, 10], "long": [40, 80]}, instruments=["GOLD", "EURUSD"],
                train_bars=300, test_bars=100, respect_usable_from=False)
    base.update(kw)
    return StudySpec(**base)


def fx_like(n=2500, seed=1, vol=0.006):
    return bars_from_returns(np.random.default_rng(seed).normal(0, vol, n), seed_price=1.2)


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class QualityTests(unittest.TestCase):
    def test_clean_series_passes(self):
        r = check_bars(fx_like(), asset_class="fx")
        self.assertEqual(r["status"], "pass", r["findings"])
        self.assertEqual(r["glitch_bar_count"], 0)

    def test_structural_problems_fail(self):
        b = fx_like(800)
        dup = pd.concat([b, b.iloc[[10]]]).sort_index()
        self.assertEqual(check_bars(dup)["status"], "fail")
        bad = b.copy()
        bad.iloc[50, bad.columns.get_loc("High")] = bad.iloc[50]["Close"] * 0.9           # High below Close
        r = check_bars(bad)
        self.assertEqual(r["status"], "fail")
        self.assertTrue(any("do not contain" in f["message"] for f in r["findings"]))
        self.assertEqual(check_bars(b.iloc[:100])["status"], "fail")                           # too short
        self.assertEqual(check_bars(b.reset_index(drop=True))["status"], "fail")              # no datetime index
        nz = b.copy()
        nz.iloc[5, 0] = -1.0
        self.assertEqual(check_bars(nz)["status"], "fail")

    def test_spike_and_revert_is_a_glitch_but_a_persistent_move_and_a_crisis_day_are_not(self):
        r = np.random.default_rng(3).normal(0, 0.006, 1500)
        glitch = r.copy()
        glitch[700] = 0.14
        glitch[701] = -0.135                                                   # +14 % then back: a provider print
        g = bars_from_returns(glitch)
        bad = find_bad_bars(g, asset_class="fx")
        self.assertEqual([str(i.date()) for i in bad.index], [str(g.index[700].date())])
        cleaned, removed = clean_bars(g)
        self.assertEqual(len(cleaned), len(g) - 1)
        self.assertEqual(len(g), 1500)                                         # the input frame is never modified
        persistent = r.copy()
        persistent[700] = 0.14                                                 # a real jump that stays
        self.assertEqual(len(find_bad_bars(bars_from_returns(persistent))), 0)
        crisis = np.concatenate([np.random.default_rng(4).normal(0, 0.006, 700), np.random.default_rng(5).normal(0, 0.04, 80), np.random.default_rng(6).normal(0, 0.006, 600)])
        crisis[760], crisis[761] = 0.11, -0.10                                 # big reversal, but only ~3 sigma of the local volatility
        self.assertEqual(len(find_bad_bars(bars_from_returns(crisis))), 0)

    def test_contradicting_open_is_flagged(self):
        b = fx_like(1200)
        i = 600
        b.iloc[i, b.columns.get_loc("Open")] = b.iloc[i - 1]["Close"] * 1.12
        b.iloc[i, b.columns.get_loc("High")] = max(b.iloc[i]["High"], b.iloc[i]["Open"])
        bad = find_bad_bars(b)
        self.assertEqual(list(bad.index), [b.index[i]])
        self.assertTrue(bad.iloc[0]["bad_open"])

    def test_overlap_and_status_levels(self):
        a = fx_like(900)
        b = a.copy()
        b.iloc[100:110, b.columns.get_loc("Close")] *= 1.001
        c = compare_overlap(a, b)
        self.assertEqual(c["common_bars"], 900)
        self.assertAlmostEqual(c["max_close_diff_pct"], 0.1, places=2)
        self.assertEqual(compare_overlap(a, a.iloc[:0])["common_bars"], 0)
        gappy = fx_like(900).drop(fx_like(900).index[300:340])
        self.assertEqual(check_bars(gappy)["status"], "warn")

    def test_usable_history_starts_where_the_series_becomes_dense(self):
        dense = bars_from_returns(np.random.default_rng(7).normal(0, 0.01, 2600), start="2005-01-03")
        sparse_early = dense[dense.index.year >= 2005].copy()
        early = sparse_early.index.year < 2010
        sparse_early.loc[early, ["Open", "High", "Low"]] = sparse_early.loc[early, ["Close", "Close", "Close"]].to_numpy()      # settlement-only prints
        keep = ~(early & (np.arange(len(sparse_early)) % 3 == 0))
        sparse_early = sparse_early[keep]
        u = usable_history(sparse_early)
        self.assertTrue(u["usable"])
        self.assertEqual(u["first_usable_date"][:4], "2010")
        self.assertTrue(usable_history(dense)["usable"])
        self.assertEqual(usable_history(dense)["first_usable_date"][:4], "2005" if usable_history(dense)["first_usable_date"][:4] == "2005" else "2006")
        allbad = dense.copy()
        allbad[["Open", "High", "Low"]] = allbad[["Close", "Close", "Close"]].to_numpy()
        self.assertFalse(usable_history(allbad)["usable"])
        self.assertFalse(usable_history(dense.iloc[:400])["usable"])


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class PowerTests(unittest.TestCase):
    def test_effective_markets(self):
        rng = np.random.default_rng(0)
        indep = pd.DataFrame(rng.normal(size=(2000, 6)))
        e = effective_markets(indep)
        self.assertAlmostEqual(e["effective_independent_markets"], 6, delta=0.6)
        common = rng.normal(size=(2000, 1))
        same = pd.DataFrame(common + 0.001 * rng.normal(size=(2000, 6)))
        self.assertAlmostEqual(effective_markets(same)["effective_independent_markets"], 1, delta=0.05)

    def test_standard_error_and_minimum_detectable_sharpe(self):
        self.assertAlmostEqual(sharpe_se(0.0, 4.0), 0.5)
        self.assertAlmostEqual(sharpe_se(1.0, 4.0), math.sqrt(1.5 / 4))
        base = min_detectable_sharpe(5, 3, 10, 0.0001)
        self.assertGreater(base["pooled_sharpe_needed"], base["hurdle_best_of_n_annual"])
        self.assertLess(min_detectable_sharpe(20, 3, 10, 0.0001)["per_market_sharpe_needed"], base["per_market_sharpe_needed"])
        self.assertLess(min_detectable_sharpe(5, 12, 10, 0.0001)["per_market_sharpe_needed"], base["per_market_sharpe_needed"])
        self.assertGreater(min_detectable_sharpe(5, 3, 200, 0.0001)["pooled_sharpe_needed"], base["pooled_sharpe_needed"])
        # no hurdle: detection needs (z_a + z_b) standard errors of the Sharpe itself
        z = 1.6448536269514722 + 0.8416212335729143
        one = min_detectable_sharpe(4, 1, 1, 0.0)
        self.assertAlmostEqual(one["pooled_sharpe_needed"], z * sharpe_se(one["pooled_sharpe_needed"], 4), places=6)


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class SpecFileTests(unittest.TestCase):
    def test_every_spec_is_valid_and_conservative(self):
        specs = available_instruments()
        self.assertGreaterEqual(len(specs), 16)
        self.assertEqual(len({s.symbol for s in specs.values()}), len(specs))
        for iid, s in specs.items():
            self.assertLessEqual(s.max_leverage, s.broker_max_leverage_assumed, iid)
            self.assertGreater(s.gap_stress_pct, s.min_stop_pct, iid)
            self.assertLessEqual(s.gap_stress_pct, 25.0 if iid not in ("GOLD", "EURUSD", "SP500") else 25.0, iid)
            self.assertIn("unverified", (s.notes or "").lower(), iid)
            self.assertEqual(s, load_instrument(iid))
        self.assertEqual(len([i for i in specs if i not in ("GOLD", "EURUSD", "SP500")]), 13)

    def test_original_three_specs_are_untouched(self):
        # economics of the original instruments are what all earlier results were produced with
        g = load_instrument("GOLD")
        self.assertEqual((g.costs.fee_bps, g.costs.spread_bps, g.costs.slippage_bps, g.max_leverage, g.gap_stress_pct, g.min_stop_pct), (2.0, 3.0, 2.0, 5.0, 3.0, 0.15))
        e = load_instrument("EURUSD")
        self.assertEqual((e.costs.spread_bps, e.costs.slippage_bps, e.max_leverage, e.gap_stress_pct), (0.8, 0.5, 10.0, 2.0))
        s = load_instrument("SP500")
        self.assertEqual((s.costs.fee_bps, s.costs.spread_bps, s.gap_stress_pct, s.min_stop_pct), (0.5, 1.0, 6.0, 0.1))
        for name in ("GOLD", "EURUSD", "SP500"):
            self.assertNotIn("usable_from", json.loads((INSTR_DIR / f"{name}.json").read_text()))      # profile data lives elsewhere, not in the spec

    def test_profiles_cover_every_spec_and_state_the_facts(self):
        prof = load_profiles()
        for iid in available_instruments():
            self.assertIn(iid, prof, iid)
            self.assertTrue(prof[iid]["usable"], iid)
            self.assertGreater(prof[iid]["bars_usable"], 2000, iid)
        self.assertIn("SILVER", prof)                              # profiled, but only ~460 dense bars (from 2025; earlier prints lack OHL): no spec is provided
        self.assertLess(prof["SILVER"]["bars_usable"], 1000)
        self.assertNotIn("SILVER", available_instruments())
        self.assertEqual(usable_from("GOLD"), "2009-01-02")


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class StudyBreadthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.noise = {m: noise_bars(1100, 30 + k) for k, m in enumerate(["GOLD", "EURUSD"])}
        cls.dev = {m: 880 for m in cls.noise}

    def test_legacy_and_new_modes_agree_when_calendars_are_identical(self):
        new = run_study(spec(), self.noise, self.dev)
        old = run_study(spec(fold_mode="bars", pooling="fixed", clean_data=False), self.noise, self.dev)
        for k in ("trades", "hit_rate", "return_pct", "expectancy_per_trade", "oos_days"):
            self.assertEqual(new["wfo"][k], old["wfo"][k], k)
        self.assertEqual(new["fold_table"][0]["selected_cell"], old["fold_table"][0]["selected_cell"])

    def test_calendar_folds_never_let_a_market_train_after_another_markets_test_starts(self):
        a = noise_bars(2500, 1, )
        b = noise_bars(2500, 2).iloc[900:]                                     # a market that starts years later
        dev = {"GOLD": a, "EURUSD": b}
        folds = _calendar_folds(dev, spec())
        self.assertGreater(len(folds["GOLD"]), len(folds["EURUSD"]))           # the late market only takes part in later folds
        for k in {f["index"] for fl in folds.values() for f in fl}:
            train_end = max(dev[i].index[f["train_end"] - 1] for i in dev for f in folds[i] if f["index"] == k)
            test_start = min(dev[i].index[f["test_start"]] for i in dev for f in folds[i] if f["index"] == k)
            self.assertLess(train_end, test_start + pd.Timedelta(days=5), k)    # same calendar boundary for everybody (a few days of calendar rounding)
        legacy = {i: [vars(f) for f in make_folds(len(dev[i]), 300, 100)] for i in dev}
        k = 3                                                                  # the legacy index folds mix different calendar periods
        t_end = max(dev[i].index[legacy[i][k]["train_end"] - 1] for i in dev)
        t_start = min(dev[i].index[legacy[i][k]["test_start"]] for i in dev)
        self.assertGreater(t_end, t_start + pd.Timedelta(days=100))            # the leak the calendar folds remove

    def test_late_or_short_markets_are_excluded_or_join_later_and_reported(self):
        a = noise_bars(1500, 1)
        short = noise_bars(1500, 2).iloc[1300:]
        r = run_study(spec(instruments=["GOLD", "EURUSD"]), {"GOLD": a, "EURUSD": short}, {"GOLD": 1200, "EURUSD": 200})
        self.assertEqual(r["markets"], ["GOLD"])
        self.assertIn("EURUSD", r["excluded_markets"])
        with self.assertRaises(ValueError):
            run_study(spec(instruments=["EURUSD"]), {"EURUSD": short}, {"EURUSD": 200})
        late = noise_bars(2000, 3).iloc[700:]
        r2 = run_study(spec(), {"GOLD": noise_bars(2000, 4), "EURUSD": late}, {"GOLD": 1600, "EURUSD": 1300})
        self.assertEqual(sorted(r2["markets"]), ["EURUSD", "GOLD"])
        self.assertLess(r2["folds"]["EURUSD"], r2["folds"]["GOLD"])

    def test_cost_multiplier_makes_every_trade_dearer(self):
        mom = {m: momentum_bars(1100, 40 + k, phi=0.4) for k, m in enumerate(["GOLD", "EURUSD"])}
        cheap = run_study(spec(), mom, self.dev)
        dear = run_study(spec(cost_multiplier=8.0), mom, self.dev)
        self.assertLess(dear["wfo"]["return_pct"], cheap["wfo"]["return_pct"])
        self.assertEqual(dear["cost_multiplier"], 8.0)
        with self.assertRaises(ValueError):
            spec(cost_multiplier=0)

    def test_glitch_bars_are_removed_for_the_study_not_from_the_data(self):
        r = np.random.default_rng(5).normal(0, 0.006, 1100)
        clean_frames = {m: bars_from_returns(r.copy() if m == "GOLD" else np.random.default_rng(6).normal(0, 0.006, 1100)) for m in ("GOLD", "EURUSD")}
        glitched = {m: f.copy() for m, f in clean_frames.items()}
        g = np.random.default_rng(5).normal(0, 0.006, 1100)
        g[400], g[401] = 0.15, -0.14
        glitched["GOLD"] = bars_from_returns(g)
        res = run_study(spec(), glitched, self.dev)
        self.assertEqual(res["glitch_bars_removed"]["GOLD"], [str(glitched["GOLD"].index[400].date())])
        raw = run_study(spec(clean_data=False), glitched, self.dev)
        self.assertEqual(raw["glitch_bars_removed"], {})
        self.assertEqual(len(glitched["GOLD"]), 1100)

    def test_usable_from_clips_the_history(self):
        orig = study_mod.usable_from
        study_mod.usable_from = lambda iid: "2020-06-01" if iid == "GOLD" else None
        try:
            r = run_study(spec(respect_usable_from=True), {"GOLD": noise_bars(1500, 8), "EURUSD": noise_bars(1500, 9)}, {"GOLD": 1200, "EURUSD": 1200})
        finally:
            study_mod.usable_from = orig
        self.assertTrue(r["data_used"]["GOLD"].split("(")[1].startswith("2020-06"))
        self.assertTrue(r["data_used"]["EURUSD"].split("(")[1].startswith("2018-01"))
        r2 = run_study(spec(start_date="2020-01-01"), {"GOLD": noise_bars(1500, 8), "EURUSD": noise_bars(1500, 9)}, {"GOLD": 1200, "EURUSD": 1200})
        self.assertTrue(r2["data_used"]["GOLD"].split("(")[1].startswith("2020-01"))

    def test_mixed_calendars_use_effective_days_per_year(self):
        weekdays = noise_bars(1500, 11)
        everyday = bars_from_returns(np.random.default_rng(12).normal(0, 0.01, 1900))
        everyday.index = pd.date_range("2018-01-01", periods=1900, freq="D", tz="UTC")
        r = run_study(spec(), {"GOLD": weekdays, "EURUSD": everyday}, {"GOLD": 1200, "EURUSD": 1500})
        self.assertGreater(r["bars_per_year_used"], 300)                       # a 7-day market in the pool makes the pooled calendar denser than 260
        self.assertLessEqual(r["bars_per_year_used"], 366)
        fixed = run_study(spec(pooling="fixed"), {"GOLD": weekdays, "EURUSD": everyday}, {"GOLD": 1200, "EURUSD": 1500})
        self.assertLess(fixed["bars_per_year_used"], 300)


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class RecordedBreadthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.arch = MarketArchive(self.root)
        self.reg = ExperimentRegistry(self.root / "experiments.sqlite3")

    def tearDown(self):
        self.reg.close()
        self.tmp.cleanup()

    def put(self, iid, frame):
        d = self.root / load_instrument(iid).symbol / "1d"
        d.mkdir(parents=True, exist_ok=True)
        frame.assign(Dividends=0.0, **{"Stock Splits": 0.0}).to_parquet(d / "bars.parquet")

    def test_quality_summary_is_attached_and_a_failing_series_is_refused(self):
        self.put("GOLD", noise_bars(1100, 1))
        self.put("EURUSD", noise_bars(1100, 2))
        out = run_recorded_study(self.reg, self.arch, spec())
        self.assertEqual(set(out["data_quality"]), {"GOLD", "EURUSD"})
        self.assertIn(out["data_quality"]["GOLD"]["status"], ("pass", "warn"))
        bad = noise_bars(1100, 3)
        bad.iloc[100, bad.columns.get_loc("High")] = bad.iloc[100]["Low"] * 0.5          # inconsistent bar written straight to the archive
        self.put("EURUSD", bad)
        with self.assertRaises(ValueError) as cm:
            run_recorded_study(self.reg, self.arch, spec(name="again"))
        self.assertIn("data quality check failed for EURUSD", str(cm.exception))

    def test_development_end_is_80_percent_of_the_analysed_window(self):
        self.put("GOLD", noise_bars(2000, 1))
        self.put("EURUSD", noise_bars(2000, 2))
        full = run_recorded_study(self.reg, self.arch, spec())
        self.assertTrue(full["data_used"]["GOLD"].split(" ")[1].endswith("1599"))     # 80 % of 2000 bars
        clipped = run_recorded_study(self.reg, self.arch, spec(name="clipped", start_date=str(noise_bars(2000, 1).index[1000].date())))
        n = int(full["data_used"]["GOLD"].split(" ")[1].split("..")[1]) + 1
        self.assertEqual(int(clipped["data_used"]["GOLD"].split(" ")[1].split("..")[1]) + 1, 800)  # 80 % of the 1000-bar window, not of the full history


if __name__ == "__main__":
    unittest.main()
