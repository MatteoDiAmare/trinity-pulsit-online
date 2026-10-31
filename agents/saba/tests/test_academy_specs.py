import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
    from market_data.archive import MarketArchive
    from market_data.laboratory import ExperimentRegistry
    from market_data.academy import (Decision, SimConfig, Simulator, SpecError, SpecStrategy, load_instrument,
                                     load_specs_dir, validate_spec)
    from market_data.academy import spec_strategy as ss
    from market_data.academy.causality import assert_causal, check_causal
    from market_data.academy.compare import compare_markets
    from market_data.academy.experiments import run_training
    from market_data.academy.strategies import STRATEGIES, Strategy
    from tests.test_academy import make_bars, random_walk
except ImportError:
    pd = None

SPEC_DIR = Path(__file__).resolve().parents[1] / "strategies"


def base_spec(**over):
    spec = {"name": "t", "indicators": {"fast": {"type": "sma", "window": 3}, "slow": {"type": "sma", "window": 6}},
            "entry_long": {"gt": ["fast", "slow"]}, "entry_short": {"lt": ["fast", "slow"]},
            "risk": {"stop_loss_pct": 2.0}}
    spec.update(over)
    return spec


class _View:
    """Minimal MarketView stand-in for indicator checks."""
    def __init__(self, close, high=None, low=None, ts="2025-01-06T12:00:00+00:00"):
        c = np.asarray(close, float)
        self.close, self.open = c, c
        self.high = c if high is None else np.asarray(high, float)
        self.low = c if low is None else np.asarray(low, float)
        self.volume = np.ones(len(c))
        self.n, self.timestamp = len(c), pd.Timestamp(ts)


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class SpecValidationTests(unittest.TestCase):
    def test_example_specs_are_valid_and_versioned_in_repo(self):
        specs = load_specs_dir(SPEC_DIR)
        self.assertGreaterEqual(len(specs), 3)
        for name, s in specs.items():
            self.assertIn("hypothesis", s, name)
            self.assertIsInstance(SpecStrategy(s).params()["spec"], dict)

    def test_rejections(self):
        bad = {
            "unknown key": base_spec(evil=1),
            "no stop": base_spec(risk={}),
            "stop zero": base_spec(risk={"stop_loss_pct": 0}),
            "stop 100": base_spec(risk={"stop_loss_pct": 100}),
            "no entry": {k: v for k, v in base_spec().items() if not k.startswith("entry")},
            "bad window": base_spec(indicators={"fast": {"type": "sma", "window": 1}}, entry_long={"gt": ["fast", 1]}),
            "huge window": base_spec(indicators={"fast": {"type": "sma", "window": 10_000}}, entry_long={"gt": ["fast", 1]}),
            "bad type": base_spec(indicators={"fast": {"type": "exec", "window": 5}}, entry_long={"gt": ["fast", 1]}),
            "bad source": base_spec(indicators={"fast": {"type": "sma", "source": "future_close", "window": 5}}, entry_long={"gt": ["fast", 1]}),
            "unknown operand": base_spec(entry_long={"gt": ["nope", 1]}),
            "code operand": base_spec(entry_long={"gt": ["__import__('os').system('x')", 1]}),
            "unknown operator": base_spec(entry_long={"shell": ["a", "b"]}),
            "two keys": base_spec(entry_long={"gt": ["close", 1], "lt": ["close", 2]}),
            "bool operand": base_spec(entry_long={"gt": ["close", True]}),
            "nan operand": base_spec(entry_long={"gt": ["close", float("nan")]}),
            "prev too far": base_spec(entry_long={"gt": [{"prev": ["close", 51]}, 1]}),
            "prev zero": base_spec(entry_long={"gt": [{"prev": ["close", 0]}, 1]}),
            "lag time feature": base_spec(entry_long={"gt": [{"prev": ["hour_utc", 1]}, 1]}),
            "indicator named like raw": base_spec(indicators={"close": {"type": "sma", "window": 3}}),
            "atr stop without atr": base_spec(risk={"stop_loss_pct": 2, "stop_atr_mult": 2, "atr_indicator": "fast"}),
            "size zero": base_spec(size=0),
            "not an object": [1, 2],
        }
        deep = {"gt": ["close", 1]}
        for _ in range(12):
            deep = {"not": deep}
        bad["too deep"] = base_spec(entry_long=deep)
        bad["too many indicators"] = base_spec(indicators={f"i{k}": {"type": "sma", "window": 3} for k in range(25)})
        for label, spec in bad.items():
            with self.assertRaises(SpecError, msg=label):
                validate_spec(copy.deepcopy(spec))

    def test_module_never_executes_dynamic_code(self):
        src = Path(ss.__file__).read_text()
        for token in ("eval(", "exec(", "__import__", "compile(", "importlib", "subprocess", "os.system", "pickle"):
            self.assertNotIn(token, src)

    def test_spec_is_copied_not_aliased(self):
        spec = base_spec()
        strat = SpecStrategy(spec)
        spec["risk"]["stop_loss_pct"] = 99
        self.assertEqual(strat.spec["risk"]["stop_loss_pct"], 2.0)


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class IndicatorTests(unittest.TestCase):
    def ev(self, spec, view):
        return ss._Eval(view, validate_spec(spec))

    def ind(self, **kw):
        return base_spec(indicators={"x": kw}, entry_long={"gt": ["x", 0]}, entry_short={"lt": ["x", 0]})

    def test_sma_ema_stdev_roc_zscore_highest_lowest(self):
        rng = np.random.default_rng(1)
        c = 100 + np.cumsum(rng.normal(0, 1, 120))
        v = _View(c)
        self.assertAlmostEqual(self.ev(self.ind(type="sma", window=10), v).indicator("x", 0), c[-10:].mean())
        self.assertAlmostEqual(self.ev(self.ind(type="stdev", window=10), v).indicator("x", 0), c[-10:].std())
        self.assertAlmostEqual(self.ev(self.ind(type="roc", window=5), v).indicator("x", 0), c[-1] / c[-6] - 1)
        z = (c[-1] - c[-20:].mean()) / c[-20:].std()
        self.assertAlmostEqual(self.ev(self.ind(type="zscore", window=20), v).indicator("x", 0), z)
        v2 = _View(c, high=c + 1, low=c - 1)
        self.assertAlmostEqual(self.ev(self.ind(type="highest", source="high", window=10), v2).indicator("x", 0), (c + 1)[-10:].max())
        self.assertAlmostEqual(self.ev(self.ind(type="highest", source="high", window=10, exclude_current=True), v2).indicator("x", 0), (c + 1)[-11:-1].max())
        self.assertAlmostEqual(self.ev(self.ind(type="lowest", source="low", window=10), v2).indicator("x", 0), (c - 1)[-10:].min())
        ema = pd.Series(c[-50:]).ewm(span=10, adjust=True).mean().iloc[-1]
        self.assertAlmostEqual(self.ev(self.ind(type="ema", window=10), v).indicator("x", 0), ema, places=9)

    def test_rsi_extremes_and_atr(self):
        up = _View(np.linspace(100, 200, 100))
        self.assertEqual(self.ev(self.ind(type="rsi", window=14), up).indicator("x", 0), 100.0)
        down = _View(np.linspace(200, 100, 100))
        self.assertAlmostEqual(self.ev(self.ind(type="rsi", window=14), down).indicator("x", 0), 0.0, places=6)
        c = np.full(80, 100.0)
        flat = _View(c, high=c + 2, low=c - 2)
        self.assertAlmostEqual(self.ev(self.ind(type="atr", window=14), flat).indicator("x", 0), 4.0)

    def test_insufficient_history_gives_nan_not_a_guess(self):
        v = _View(np.arange(1, 8.0))
        self.assertTrue(np.isnan(self.ev(self.ind(type="sma", window=20), v).indicator("x", 0)))
        self.assertTrue(np.isnan(self.ev(self.ind(type="ema", window=20), v).indicator("x", 0)))

    def test_prev_and_shift_use_only_older_bars(self):
        c = np.arange(1, 61, dtype=float)
        v = _View(c)
        e = self.ev(self.ind(type="sma", window=5), v)
        self.assertAlmostEqual(e.value({"prev": ["x", 3]}), c[:-3][-5:].mean())
        self.assertEqual(e.value({"prev": ["close", 2]}), c[-3])

    def test_cross_and_boolean_logic(self):
        up_then_down = np.concatenate([np.linspace(100, 110, 30), np.linspace(110, 100, 30)])
        spec = base_spec(indicators={"f": {"type": "sma", "window": 3}, "s": {"type": "sma", "window": 10}},
                         entry_long={"cross_above": ["f", "s"]}, entry_short={"cross_below": ["f", "s"]})
        hits = [ss._Eval(_View(up_then_down[:k]), validate_spec(copy.deepcopy(spec))).cond(spec["entry_long"]) for k in range(15, 61)]
        self.assertLessEqual(sum(hits), 1)
        e = ss._Eval(_View([1.0] * 5), validate_spec(base_spec()))
        self.assertTrue(e.cond({"all": [{"ge": [1, 1]}, {"not": {"gt": [1, 2]}}]}))
        self.assertTrue(e.cond({"any": [{"gt": [1, 2]}, {"le": [3, 3]}]}))
        self.assertFalse(e.cond({"gt": [{"div": [1, 0]}, 0]}))           # NaN never satisfies a comparison

    def test_time_features(self):
        e = ss._Eval(_View([1.0] * 5, ts="2025-01-10T08:00:00+00:00"), validate_spec(base_spec()))
        self.assertEqual(e.value("hour_utc"), 8.0)
        self.assertEqual(e.value("weekday"), 4.0)


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class SpecStrategyBehaviourTests(unittest.TestCase):
    def test_every_decision_carries_a_stop_and_respects_cap(self):
        for name, spec in load_specs_dir(SPEC_DIR).items():
            res = Simulator(SimConfig(1000, max_exposure=None, instrument=load_instrument("EURUSD"))).run(
                random_walk(400, seed=3), SpecStrategy(spec))
            acts = [d for d in res.decisions if d["action"] in ("BUY", "SELL")]
            self.assertTrue(acts, name)
            cap = spec["risk"]["stop_loss_pct"]
            for d in acts:
                self.assertIsNotNone(d["stop_loss_pct"])
                self.assertLessEqual(d["stop_loss_pct"], cap + 1e-9)
            self.assertFalse([o for o in res.orders if o["kind"] == "rejected" and o["reason"] == "stop_loss_required"])

    def test_entry_exit_reverse_and_conflict(self):
        c = np.concatenate([np.linspace(100, 90, 20), np.linspace(90, 110, 20)])
        bars = make_bars([(x, x, x, x) for x in c])
        with_exits = base_spec(exit_long={"lt": ["fast", "slow"]}, exit_short={"gt": ["fast", "slow"]}, allow_short=True)
        res = Simulator(SimConfig(1000)).run(bars, SpecStrategy(with_exits))
        acts = [(d["action"], d["reason"]) for d in res.decisions if d["action"] != "ABSTAIN"]
        self.assertEqual(acts[:2], [("SELL", "spec_entry"), ("CLOSE", "spec_exit")])
        res = Simulator(SimConfig(1000)).run(bars, SpecStrategy(base_spec(allow_short=True)))
        acts = [(d["action"], d["reason"]) for d in res.decisions if d["action"] != "ABSTAIN"]
        self.assertEqual(acts[0], ("SELL", "spec_entry"))
        self.assertIn(("BUY", "spec_reverse"), acts)
        both = base_spec(entry_long={"gt": ["close", 0]}, entry_short={"gt": ["close", 0]})
        res = Simulator(SimConfig(1000)).run(bars, SpecStrategy(both))
        self.assertFalse([d for d in res.decisions if d["action"] != "ABSTAIN"])        # conflicting signals => abstain

    def test_atr_stop_never_wider_than_cap_and_floor_applies(self):
        spec = base_spec(indicators={"atr": {"type": "atr", "window": 5}},
                         entry_long={"gt": ["close", 0]}, entry_short={"lt": ["close", 0]},
                         risk={"stop_loss_pct": 5.0, "stop_atr_mult": 2.0, "atr_indicator": "atr", "stop_floor_pct": 0.5})
        quiet = make_bars([(100, 100.01, 99.99, 100)] * 60)
        wild = make_bars([(100, 120, 80, 100)] * 60)
        for bars, expect in ((quiet, 0.5), (wild, 5.0)):
            res = Simulator(SimConfig(1000)).run(bars, SpecStrategy(spec))
            stops = {round(d["stop_loss_pct"], 6) for d in res.decisions if d["action"] == "BUY"}
            self.assertEqual(stops, {expect})

    def test_registered_and_buildable_via_params(self):
        from market_data.academy import build_strategy
        s = build_strategy("spec", {"spec": base_spec()})
        self.assertIsInstance(s, SpecStrategy)
        self.assertEqual(build_strategy("spec", s.params()).spec, s.spec)


class _Coin(Strategy):
    """Non-deterministic: unseeded randomness."""
    name = "coin"

    def params(self):
        return {}

    def on_bar(self, view, position):
        return Decision("BUY", stop_loss_pct=2.0) if np.random.random() < 0.3 and position is None else Decision()


class _Leaky(Strategy):
    """Decisions depend on how many times ANY instance has been called (hidden global state)."""
    name = "leaky"
    CALLS = 0

    def params(self):
        return {}

    def on_bar(self, view, position):
        _Leaky.CALLS += 1
        return Decision("BUY", stop_loss_pct=2.0) if (_Leaky.CALLS // 7) % 2 == 0 and position is None else Decision()


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class CausalityGateTests(unittest.TestCase):
    def setUp(self):
        self.bars = random_walk(400, seed=5)
        STRATEGIES["coin"], STRATEGIES["leaky"] = _Coin, _Leaky

    def tearDown(self):
        STRATEGIES.pop("coin", None)
        STRATEGIES.pop("leaky", None)

    def test_gate_passes_honest_strategies(self):
        for name, spec in load_specs_dir(SPEC_DIR).items():
            r = assert_causal("spec", {"spec": spec}, self.bars)
            self.assertTrue(r["ok"], name)
        self.assertTrue(check_causal("sma_cross", {"short": 5, "long": 20}, self.bars)["ok"])

    def test_gate_catches_nondeterminism_and_hidden_state(self):
        for name in ("coin", "leaky"):
            r = check_causal(name, {}, self.bars)
            self.assertFalse(r["ok"], name)
            self.assertFalse(r["checks"]["deterministic"], name)
            with self.assertRaises(ValueError):
                assert_causal(name, {}, self.bars)

    def test_gate_refuses_when_too_little_data(self):
        self.assertFalse(check_causal("sma_cross", {}, self.bars.iloc[:30])["ok"])

    def test_view_cannot_reach_future_bars(self):
        from market_data.academy.strategies import MarketView
        seen = []

        class Probe(Strategy):
            name = "probe"
            def params(self):
                return {}
            def on_bar(self, view, position):
                for attr in ("open", "high", "low", "close", "volume"):
                    a = getattr(view, attr)
                    seen.append((a.base is not None and a.base.shape[1] <= view.n and a.base.base is None, a.flags.writeable, len(a) <= view.n))
                    with self_.assertRaises(ValueError):
                        a[-1] = 0.0
                return Decision()
        self_ = self
        Simulator(SimConfig()).run(self.bars.iloc[:80], Probe())
        self.assertTrue(seen and all(x == (True, False, True) for x in seen))
        arrays = {k: np.arange(5000.0) for k in ("open", "high", "low", "close", "volume")}
        v = MarketView(arrays, pd.date_range("2020", periods=5000, freq="min", tz="UTC"), 4000)
        self.assertEqual((v.n, v.total), (3000, 4000))
        self.assertEqual(v.close[-1], 3999.0)

    def test_training_rejects_unstable_strategy_before_recording_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ExperimentRegistry(Path(tmp) / "r.db")
            plan = [{"strategy": "coin", "params": {}, "label": "unstable"},
                    {"strategy": "spec", "params": {"spec": base_spec()}, "label": "honest"}]
            out = run_training(self.bars, reg, symbol="T", interval="1d", plan=plan, control_runs=0)
            self.assertEqual([r["label"] for r in out["rejected_strategies"]], ["unstable"])
            names = [r["name"] for r in reg.recent(100)]
            self.assertNotIn("academy:coin", names)
            self.assertIn("academy:spec", names)
            rec = [r for r in reg.recent(100) if r["name"] == "academy:spec"][0]
            self.assertTrue(rec["configuration"]["causality_checked"])
            self.assertEqual(rec["configuration"]["label"], "honest")
            reg.close()


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class CompareMarketsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.reg = ExperimentRegistry(self.root / "r.db")
        self.archive = MarketArchive(self.root / "archive")
        for sym, seed in (("GC=F", 1), ("EURUSD=X", 2), ("ES=F", 3)):
            self.put(sym, random_walk(700, seed=seed))
        self.plan = [{"strategy": "spec", "params": {"spec": s}, "label": n} for n, s in load_specs_dir(SPEC_DIR).items()]
        self.plan.append({"strategy": "sma_cross", "params": {"short": 8, "long": 30}, "label": "sma"})

    def put(self, sym, bars):
        d = self.root / "archive" / sym / "1d"
        d.mkdir(parents=True, exist_ok=True)
        bars.assign(Dividends=0.0, **{"Stock Splits": 0.0}).to_parquet(d / "bars.parquet")

    def tearDown(self):
        self.reg.close()
        self.tmp.cleanup()

    def specs(self, *ids):
        return [load_instrument(i) for i in ids]

    def test_matrix_over_three_markets(self):
        out = compare_markets(self.reg, self.archive, self.plan, self.specs("GOLD", "EURUSD", "SP500"))
        self.assertEqual(out["markets"], ["GOLD", "EURUSD", "SP500"])
        self.assertEqual(set(out["matrix"]), {"ema_trend", "rsi_reversion", "channel_breakout_filtered", "sma"})
        for label, m in out["matrix"].items():
            self.assertEqual(m["markets_evaluated"], 3)
            for iid, cell in m["per_market"].items():
                self.assertEqual(cell["status"], "ok")
                self.assertEqual(cell["validation"]["shortfall"], 0.0)
                self.assertIn("cost_per_trade_pct_of_capital", cell["validation"])
        self.assertEqual(set(out["best_strategy_per_market"]), {"GOLD", "EURUSD", "SP500"})
        self.assertIn("markets above the luck benchmark", out["transfer"]["sma"])
        # each cell is a recorded, counted trial on its own market
        for iid, info in out["market_data"].items():
            self.assertEqual(info["trials_total_on_market"], 4)

    def test_second_run_reuses_and_does_not_recount(self):
        compare_markets(self.reg, self.archive, self.plan, self.specs("GOLD", "EURUSD"))
        before = self.reg.db.execute("SELECT COUNT(*) FROM experiments WHERE name='academy:spec' OR name='academy:sma_cross'").fetchone()[0]
        out = compare_markets(self.reg, self.archive, self.plan, self.specs("GOLD", "EURUSD"))
        after = self.reg.db.execute("SELECT COUNT(*) FROM experiments WHERE name='academy:spec' OR name='academy:sma_cross'").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(out["market_data"]["GOLD"]["trials_total_on_market"], 4)

    def test_comparison_never_reads_the_final_test_region(self):
        a = compare_markets(self.reg, self.archive, self.plan[:2], self.specs("GOLD", "EURUSD"))
        tampered = random_walk(700, seed=1)
        tampered.iloc[560:, :4] *= 40                       # wreck GOLD's test region (last 20 %)
        self.put("GC=F", tampered)
        with tempfile.TemporaryDirectory() as tmp2:
            reg2 = ExperimentRegistry(Path(tmp2) / "r.db")
            b = compare_markets(reg2, self.archive, self.plan[:2], self.specs("GOLD", "EURUSD"))
            reg2.close()
        for label in a["matrix"]:
            self.assertEqual(a["matrix"][label]["per_market"]["GOLD"]["validation"], b["matrix"][label]["per_market"]["GOLD"]["validation"])

    def test_validation_and_missing_data(self):
        with self.assertRaises(ValueError):
            compare_markets(self.reg, self.archive, self.plan, self.specs("GOLD"))
        dup = self.plan + [dict(self.plan[0])]
        with self.assertRaises(ValueError):
            compare_markets(self.reg, self.archive, dup, self.specs("GOLD", "EURUSD"))
        (self.root / "archive" / "ES=F" / "1d" / "bars.parquet").unlink()
        out = compare_markets(self.reg, self.archive, self.plan[:1], self.specs("GOLD", "EURUSD", "SP500"))
        self.assertIn("SP500", out["skipped_markets"])
        self.assertEqual(out["markets"], ["GOLD", "EURUSD"])

    def test_cheating_strategy_shows_as_rejected_cell(self):
        STRATEGIES["coin"] = _Coin
        try:
            plan = [{"strategy": "coin", "params": {}, "label": "cheat"}, self.plan[3]]
            out = compare_markets(self.reg, self.archive, plan, self.specs("GOLD", "EURUSD"))
        finally:
            STRATEGIES.pop("coin", None)
        self.assertEqual(out["matrix"]["cheat"]["per_market"]["GOLD"]["status"], "rejected_by_causality_gate")
        self.assertEqual(out["matrix"]["cheat"]["markets_evaluated"], 0)


if __name__ == "__main__":
    unittest.main()
