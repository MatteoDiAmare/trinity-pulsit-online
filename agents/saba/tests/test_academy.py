import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
    from market_data.laboratory import ExperimentRegistry
    from market_data.academy import CostModel, SimConfig, Simulator, Decision, build_strategy, RiskProfile
    from market_data.academy.strategies import Strategy
    from market_data.academy.execution import resolve_bar_exit
    from market_data.academy.evaluation import compute_metrics
    from market_data.academy.experiments import Split, run_training, run_final_test, plan_experiments
except ImportError:
    pd = None


def make_bars(rows, start="2025-01-01"):
    """rows: list of (open, high, low, close)."""
    idx = pd.date_range(start, periods=len(rows), freq="D", tz="UTC")
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)
    df["Volume"] = 1.0
    return df


def random_walk(n=400, seed=1):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    open_ = np.concatenate([[100], close[:-1]])
    high = np.maximum(open_, close) * 1.003
    low = np.minimum(open_, close) * 0.997
    return make_bars(list(zip(open_, high, low, close)))


class Scripted(Strategy):
    """Emits scripted decisions by bar number; records what it saw."""
    name = "scripted"

    def __init__(self, script):
        self.script, self.seen_lengths, self.last_close = script, [], []

    def on_bar(self, view, position):
        self.seen_lengths.append(len(view))
        self.last_close.append(view.close[-1])
        return self.script.get(len(view) - 1, Decision())


FREE = CostModel(0, 0, 0)
# Mandatory stop-loss stays in force; only the sizing/halt LIMITS are opened so that fixed-price
# fixtures can assert exact fills. Risk behaviour itself is tested in test_academy_risk.py.
LOOSE = RiskProfile(risk_per_trade_pct=100, max_worst_case_loss_pct=100, daily_loss_limit_pct=100,
                    max_drawdown_halt_pct=100, max_consecutive_losses=10**6)


def BUY(**kw):
    return Decision("BUY", **{"stop_loss_pct": 50.0, **kw})


def SELL(**kw):
    return Decision("SELL", **{"stop_loss_pct": 50.0, **kw})


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class SimulatorTests(unittest.TestCase):
    def run_sim(self, bars, script, costs=FREE, capital=1000.0, **kw):
        strat = Scripted(script)
        res = Simulator(SimConfig(capital, costs, risk=LOOSE, **kw)).run(bars, strat)
        return res, strat

    # ---- look-ahead ----
    def test_strategy_never_sees_future_bars(self):
        bars = random_walk(60)
        res, strat = self.run_sim(bars, {})
        self.assertEqual(strat.seen_lengths, list(range(1, 60)))  # last bar: no decision
        np.testing.assert_array_equal(strat.last_close, bars["Close"].to_numpy()[:59])

    def test_view_is_read_only(self):
        class Tamper(Strategy):
            def on_bar(self, view, position):
                with self_.assertRaises(ValueError):
                    view.close[-1] = 1.0
                return Decision()
        self_ = self
        Simulator().run(random_walk(10), Tamper())

    def test_decisions_invariant_to_future_data(self):
        bars = random_walk(300)
        for name, params in (("sma_cross", {"short": 5, "long": 20}), ("breakout", {"window": 15}),
                             ("mean_reversion", {"window": 20, "z_entry": 1.5}),
                             ("random_control", {"seed": 3, "trade_prob": 0.1})):
            full = Simulator().run(bars, build_strategy(name, params))
            cut = Simulator().run(bars.iloc[:200], build_strategy(name, params))
            f = [d for d in full.decisions if d["timestamp"] < bars.index[198]]
            c = [d for d in cut.decisions if d["timestamp"] < bars.index[198]]
            self.assertEqual(f, c, name)
        # Corrupting the future must not change past decisions.
        tampered = bars.copy()
        tampered.iloc[150:, :4] *= 3
        a = Simulator().run(bars, build_strategy("sma_cross", {"short": 5, "long": 20}))
        b = Simulator().run(tampered, build_strategy("sma_cross", {"short": 5, "long": 20}))
        self.assertEqual([d for d in a.decisions if d["timestamp"] < bars.index[149]],
                         [d for d in b.decisions if d["timestamp"] < bars.index[149]])

    def test_fill_happens_at_next_open_not_signal_close(self):
        bars = make_bars([(100, 101, 99, 100), (110, 111, 109, 110), (110, 111, 109, 110)])
        res, _ = self.run_sim(bars, {0: BUY()})
        entry = [o for o in res.orders if o["kind"] == "entry"][0]
        self.assertEqual(entry["fill_price"], 110)
        self.assertEqual(entry["timestamp"], bars.index[1])

    def test_trade_window_does_not_read_beyond_end(self):
        bars = random_walk(100)
        strat = Scripted({})
        Simulator().run(bars, strat, trade_start=50, trade_end=80)
        self.assertEqual(max(strat.seen_lengths), 79)
        self.assertEqual(min(strat.seen_lengths), 51)

    # ---- costs ----
    def test_costs_applied_to_both_fills(self):
        bars = make_bars([(100, 100, 100, 100)] * 4)
        costs = CostModel(fee_bps=10, spread_bps=20, slippage_bps=10)
        res, _ = self.run_sim(bars, {0: BUY(), 1: Decision("CLOSE")}, costs)
        t = res.trades[0]
        self.assertAlmostEqual(t["entry_price"], 100 * (1 + 0.0020), places=9)   # half spread 10bps + slip 10bps
        self.assertAlmostEqual(t["exit_price"], 100 * (1 - 0.0020), places=9)
        self.assertGreater(t["fees"], 0)
        self.assertLess(t["net_pnl"], 0)
        self.assertAlmostEqual(t["net_pnl"], t["gross_pnl"] - t["fees"], places=9)
        self.assertGreater(t["spread_slippage_cost"], 0)

    def test_more_costs_never_help(self):
        bars = random_walk(300)
        s = lambda: build_strategy("sma_cross", {"short": 5, "long": 20})
        free = Simulator(SimConfig(costs=FREE, risk=LOOSE)).run(bars, s())
        costly = Simulator(SimConfig(costs=CostModel(10, 10, 10), risk=LOOSE)).run(bars, s())
        self.assertGreater(free.equity.equity.iloc[-1], costly.equity.equity.iloc[-1])

    def test_negative_costs_rejected(self):
        with self.assertRaises(ValueError):
            CostModel(-1, 0, 0)

    # ---- stops ----
    def test_stop_loss_long(self):
        bars = make_bars([(100, 100, 100, 100), (100, 101, 100, 100), (100, 100, 97, 98), (98, 98, 98, 98)])
        res, _ = self.run_sim(bars, {0: BUY(stop_loss_pct=2)})
        t = res.trades[0]
        self.assertEqual(t["exit_reason"], "stop_loss")
        self.assertAlmostEqual(t["exit_price"], 98.0)
        self.assertAlmostEqual(res.equity.equity.iloc[-1], 1000 * 0.98, places=6)

    def test_stop_loss_short(self):
        bars = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 103, 100, 102), (102, 102, 102, 102)])
        res, _ = self.run_sim(bars, {0: SELL(stop_loss_pct=2)})
        self.assertEqual(res.trades[0]["exit_reason"], "stop_loss")
        self.assertAlmostEqual(res.trades[0]["exit_price"], 102.0)
        self.assertLess(res.trades[0]["net_pnl"], 0)

    def test_take_profit_long_and_short(self):
        up = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 106, 100, 105), (105, 105, 105, 105)])
        res, _ = self.run_sim(up, {0: BUY(take_profit_pct=5)})
        self.assertEqual(res.trades[0]["exit_reason"], "take_profit")
        self.assertAlmostEqual(res.trades[0]["exit_price"], 105.0)
        down = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 94, 95), (95, 95, 95, 95)])
        res, _ = self.run_sim(down, {0: SELL(take_profit_pct=5)})
        self.assertEqual(res.trades[0]["exit_reason"], "take_profit")
        self.assertAlmostEqual(res.trades[0]["exit_price"], 95.0)
        self.assertGreater(res.trades[0]["net_pnl"], 0)

    def test_same_bar_stop_and_take_profit_is_conservative_and_flagged(self):
        bars = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 110, 90, 100), (100, 100, 100, 100)])
        res, _ = self.run_sim(bars, {0: BUY(stop_loss_pct=5, take_profit_pct=5)})
        t = res.trades[0]
        self.assertEqual(t["exit_reason"], "stop_loss")
        self.assertTrue(t["ambiguous"])
        self.assertEqual(compute_metrics(res)["ambiguous_trades"], 1)

    def test_entry_bar_can_trigger_stop(self):
        bars = make_bars([(100, 100, 100, 100), (100, 100, 90, 95), (95, 95, 95, 95)])
        res, _ = self.run_sim(bars, {0: BUY(stop_loss_pct=5)})
        self.assertEqual(res.trades[0]["exit_reason"], "stop_loss")
        self.assertEqual(res.trades[0]["exit_ts"], bars.index[1])

    def test_gap_through_stop_fills_at_open_not_stop(self):
        bars = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (90, 91, 89, 90), (90, 90, 90, 90)])
        res, _ = self.run_sim(bars, {0: BUY(stop_loss_pct=2)})
        self.assertEqual(res.trades[0]["exit_reason"], "stop_gap")
        self.assertEqual(res.trades[0]["exit_price"], 90.0)

    def test_gap_through_take_profit_fills_at_open(self):
        bars = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (110, 111, 109, 110), (110, 110, 110, 110)])
        res, _ = self.run_sim(bars, {0: BUY(take_profit_pct=5)})
        self.assertEqual(res.trades[0]["exit_reason"], "take_profit_gap")
        self.assertEqual(res.trades[0]["exit_price"], 110.0)

    def test_resolve_bar_exit_unit(self):
        self.assertIsNone(resolve_bar_exit(1, 95, 105, 100, 101, 99))
        self.assertEqual(resolve_bar_exit(-1, 105, 95, 100, 106, 99)[0], "stop_loss")

    # ---- positions / capital ----
    def test_open_position_closed_at_end_of_data(self):
        bars = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 100, 110)])
        res, _ = self.run_sim(bars, {0: BUY()})
        self.assertEqual(res.trades[0]["exit_reason"], "end_of_data")
        self.assertAlmostEqual(res.equity.equity.iloc[-1], 1100.0)

    def test_equity_equals_capital_plus_net_trade_pnl(self):
        bars = random_walk(500, seed=5)
        for costs in (FREE, CostModel(5, 8, 4)):
            res = Simulator(SimConfig(1000, costs, max_exposure=1.5, risk=LOOSE)).run(
                bars, build_strategy("breakout", {"window": 10, "stop_loss_pct": 1.0, "take_profit_pct": 2.0}))
            self.assertGreater(len(res.trades), 5)
            self.assertAlmostEqual(res.equity.equity.iloc[-1], 1000 + sum(t["net_pnl"] for t in res.trades), places=6)
            self.assertEqual(res.equity.position_side.iloc[-1], 0)

    def test_max_exposure_limits_notional(self):
        bars = make_bars([(100, 100, 100, 100)] * 4)
        res, _ = self.run_sim(bars, {0: BUY()}, max_exposure=0.5)
        self.assertAlmostEqual(res.orders[0]["notional"], 500.0)
        res, _ = self.run_sim(bars, {0: BUY(size=0.5)}, max_exposure=0.5)
        self.assertAlmostEqual(res.orders[0]["notional"], 250.0)
        res, _ = self.run_sim(bars, {0: BUY()}, CostModel(50, 0, 0), max_exposure=1.0)
        e = res.orders[0]
        self.assertLessEqual(e["notional"] + e["fee"], 1000.0 + 1e-9)  # fees fit inside budget

    def test_insufficient_capital_rejected_and_recorded(self):
        bars = make_bars([(100, 100, 100, 100)] * 4)
        res, _ = self.run_sim(bars, {0: BUY()}, capital=0.5, min_notional=1.0)
        self.assertEqual(res.trades, [])
        self.assertEqual(res.orders[0]["kind"], "rejected")
        self.assertEqual(res.orders[0]["reason"], "insufficient_capital")
        self.assertEqual(compute_metrics(res)["rejected_orders"], 1)

    def test_ruin_halts_trading(self):
        bars = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 1, 1), (1, 1, 1, 1), (1, 1, 1, 1)])
        res, _ = self.run_sim(bars, {0: BUY(), 2: BUY()}, max_exposure=3.0)
        self.assertIsNotNone(res.halted)
        self.assertGreaterEqual(res.equity.equity.min(), 0.0)

    def test_short_disabled_and_same_side_ignored(self):
        bars = make_bars([(100, 100, 100, 100)] * 5)
        res, _ = self.run_sim(bars, {0: SELL()}, allow_short=False)
        self.assertEqual(res.orders[0]["reason"], "short_selling_disabled")
        res, _ = self.run_sim(bars, {0: BUY(), 1: BUY()})
        self.assertEqual([o["reason"] for o in res.orders if o["kind"] == "rejected"], ["already_positioned"])

    def test_reversal_closes_then_opens(self):
        bars = make_bars([(100, 100, 100, 100)] * 5)
        res, _ = self.run_sim(bars, {0: BUY(), 1: SELL()})
        self.assertEqual(len(res.trades), 2)
        self.assertEqual(res.trades[0]["exit_reason"], "reversal")
        self.assertEqual(res.trades[1]["side"], -1)

    def test_every_decision_logged(self):
        bars = random_walk(100)
        res = Simulator().run(bars, build_strategy("sma_cross", {"short": 3, "long": 10}))
        self.assertEqual(len(res.decisions), 99)
        self.assertEqual(len(res.equity), 100)

    def test_invalid_decision_rejected(self):
        for kw in ({"action": "YOLO"}, {"size": 0}, {"stop_loss_pct": 0}, {"take_profit_pct": 120}):
            with self.assertRaises(ValueError):
                Decision(**kw)


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class MetricsTests(unittest.TestCase):
    def test_metrics_known_values(self):
        bars = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 100, 120),
                          (120, 120, 120, 60), (60, 60, 60, 60)])
        strat = Scripted({0: BUY(), 2: Decision("CLOSE")})
        res = Simulator(SimConfig(1000, FREE, risk=LOOSE)).run(bars, strat)
        m = compute_metrics(res)
        self.assertEqual(m["trades"], 1)
        self.assertAlmostEqual(m["net_return_pct"], 20.0)
        self.assertEqual(m["hit_rate"], 1.0)
        self.assertAlmostEqual(m["max_drawdown_pct"], 0.0)

    def test_max_drawdown(self):
        bars = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 100, 150),
                          (150, 150, 150, 75), (75, 75, 75, 75)])
        res = Simulator(SimConfig(1000, FREE, risk=LOOSE)).run(bars, Scripted({0: BUY()}))
        m = compute_metrics(res)
        self.assertAlmostEqual(m["max_drawdown_pct"], -50.0)
        self.assertAlmostEqual(m["net_return_pct"], -25.0)

    def test_no_trades_metrics_are_finite(self):
        m = compute_metrics(Simulator().run(random_walk(50), Scripted({})))
        self.assertEqual(m["trades"], 0)
        self.assertIsNone(m["hit_rate"])
        self.assertEqual(m["net_return_pct"], 0.0)


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bars = random_walk(600, seed=9)

    def tearDown(self):
        self.tmp.cleanup()

    def registry(self, name="r.db"):
        return ExperimentRegistry(Path(self.tmp.name) / name)

    def train(self, reg, bars=None, **kw):
        return run_training(self.bars if bars is None else bars, reg, symbol="TEST", interval="1d",
                            experiments=kw.pop("experiments", 12), seed=kw.pop("seed", 7), control_runs=5, **kw)

    def test_split_is_chronological_and_validated(self):
        s = Split.chronological(1000)
        self.assertEqual((s.train_end, s.val_end), (600, 800))
        with self.assertRaises(ValueError):
            Split.chronological(40)
        with self.assertRaises(ValueError):
            Split.chronological(1000, 0.7, 0.4)

    def test_reproducible_with_same_seed(self):
        r1, r2 = self.registry("a.db"), self.registry("b.db")
        a, b = self.train(r1), self.train(r2)
        strip = lambda rows: [(r["name"], r["dataset_sha256"], r["configuration"], r["results"]) for r in reversed(rows)]
        self.assertEqual(strip(r1.recent(100)), strip(r2.recent(100)))
        self.assertEqual(a["selected"], b["selected"])
        c = self.train(self.registry("c.db"), seed=8)
        self.assertNotEqual(plan_experiments(12, 7), plan_experiments(12, 8))
        r1.close(), r2.close()

    def test_training_never_uses_final_test_bars(self):
        reg = self.registry()
        split = Split.chronological(len(self.bars))
        tampered = self.bars.copy()
        tampered.iloc[split.val_end:, :4] *= 50     # wreck the untouched test period
        a = self.train(reg, self.bars)
        b = self.train(self.registry("t.db"), tampered)
        self.assertEqual(a["selected"], b["selected"])
        self.assertEqual(a["top5"], b["top5"])
        # Registry dataset hash covers only train+validation bars.
        self.assertEqual({r["dataset_sha256"] for r in reg.recent(100)}.__len__(), 1)
        reg.close()

    def test_training_ledger_stays_inside_dev_period(self):
        from market_data.academy import experiments as ex
        seen = []
        original = ex.Simulator.run

        def spy(self_, bars, strategy, *, trade_start=0, trade_end=None):
            seen.append(len(bars) if trade_end is None else trade_end)
            return original(self_, bars, strategy, trade_start=trade_start, trade_end=trade_end)
        ex.Simulator.run = spy
        try:
            reg = self.registry()
            self.train(reg)
        finally:
            ex.Simulator.run = original
        self.assertLessEqual(max(seen), Split.chronological(len(self.bars)).val_end)
        reg.close()

    def test_registry_records_experiments(self):
        reg = self.registry()
        out = self.train(reg)
        rows = reg.recent(100)
        self.assertEqual(len(rows), 12 + 1)  # experiments + summary
        self.assertTrue(all(r["name"].startswith("academy:") for r in rows))
        self.assertEqual(rows[0]["id"], out["summary_experiment_id"])
        reg.close()

    def test_final_test_runs_once_on_unseen_period(self):
        reg = self.registry()
        out = self.train(reg, experiments=20)
        exp_id = out["selected_experiment_id"]
        if exp_id is None:
            self.skipTest("no experiment met the minimum trade count")
        res, ledger = run_final_test(self.bars, reg, exp_id, keep_result=True)
        split = Split.chronological(len(self.bars))
        self.assertEqual(ledger.equity.index[0], self.bars.index[split.val_end])
        self.assertEqual(res["final_test"]["bars"], len(self.bars) - split.val_end)
        with self.assertRaises(ValueError):
            run_final_test(self.bars, reg, exp_id)
        with self.assertRaises(TypeError):
            run_final_test(self.bars, reg, exp_id, force=True)      # no override exists
        with self.assertRaises(ValueError):
            run_final_test(self.bars, reg, out["summary_experiment_id"])
        reg.close()

    def test_final_test_detects_changed_history(self):
        reg = self.registry()
        out = self.train(reg, experiments=20)
        if out["selected_experiment_id"] is None:
            self.skipTest("no experiment met the minimum trade count")
        with self.assertRaises(ValueError):
            run_final_test(self.bars.iloc[10:], reg, out["selected_experiment_id"])
        reg.close()

    def test_random_control_is_deterministic_per_seed(self):
        a = Simulator().run(self.bars, build_strategy("random_control", {"seed": 1}))
        b = Simulator().run(self.bars, build_strategy("random_control", {"seed": 1}))
        c = Simulator().run(self.bars, build_strategy("random_control", {"seed": 2}))
        self.assertEqual(a.trades, b.trades)
        self.assertNotEqual(a.trades, c.trades)

    def test_cli_train_and_final_test(self):
        import contextlib, io, json
        from market_data.archive import MarketArchive
        from market_data import academy_cli
        root = Path(self.tmp.name) / "archive"
        path = root / "TEST" / "1d"
        path.mkdir(parents=True)
        self.bars.assign(Dividends=0.0, **{"Stock Splits": 0.0}).to_parquet(path / "bars.parquet")
        def call(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = academy_cli.main(["--root", str(root), *argv])
            return code, json.loads(buf.getvalue())
        code, out = call("train", "--symbol", "TEST", "--experiments", "10", "--control-runs", "3", "--capital", "1000")
        self.assertEqual(code, 0)
        if out["selected_experiment_id"] is not None:
            code, res = call("final-test", "--symbol", "TEST", "--experiment-id", str(out["selected_experiment_id"]),
                             "--ledger-dir", str(Path(self.tmp.name) / "ledger"))
            self.assertEqual(code, 0)
            self.assertTrue((Path(self.tmp.name) / "ledger" / "equity.csv").exists())
        code, err = call("final-test", "--symbol", "TEST", "--experiment-id", "9999")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
