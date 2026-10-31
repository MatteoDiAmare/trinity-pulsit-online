import dataclasses
import unittest

try:
    import numpy as np
    import pandas as pd
    from market_data.academy import (CostModel, Decision, RiskProfile, SimConfig, Simulator, StressModel,
                                     available_instruments, build_strategy, load_instrument)
    from market_data.academy.evaluation import compute_metrics
    from market_data.academy.experiments import plan_experiments
    from market_data.academy.instruments import InstrumentSpec
    from market_data.academy.risk import size_position
    from market_data.academy.strategies import Strategy, SmaCross
    from market_data.academy.stress import inject_gaps
    from tests.test_academy import make_bars, random_walk, FREE, LOOSE, Scripted
except ImportError:
    pd = None


def hourly(rows, start="2025-01-06"):
    idx = pd.date_range(start, periods=len(rows), freq="h", tz="UTC")
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)
    df["Volume"] = 1.0
    return df


class AlwaysBuy(Strategy):
    name = "always_buy"

    def __init__(self, stop=1.0):
        self.stop = stop

    def on_bar(self, view, position):
        return Decision("BUY", stop_loss_pct=self.stop) if position is None else Decision()


GOLD = EUR = SP = None
if pd is not None:
    GOLD, EUR, SP = (load_instrument(x) for x in ("GOLD", "EURUSD", "SP500"))


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class InstrumentTests(unittest.TestCase):
    def test_three_instruments_available_and_consistent(self):
        specs = available_instruments()
        self.assertEqual(set(specs), {"GOLD", "EURUSD", "SP500"})
        self.assertEqual([specs[k].symbol for k in ("GOLD", "EURUSD", "SP500")], ["GC=F", "EURUSD=X", "ES=F"])
        for s in specs.values():
            self.assertLessEqual(s.max_leverage, s.broker_max_leverage_assumed)   # never above assumed broker cap
            self.assertGreater(s.gap_stress_pct, s.min_stop_pct)

    def test_lookup_by_symbol_roundtrip_and_hash(self):
        self.assertEqual(load_instrument("ES=F").id, "SP500")
        spec = load_instrument("GOLD")
        self.assertEqual(InstrumentSpec.from_dict(spec.to_dict()), spec)
        self.assertEqual(spec.spec_sha256, load_instrument("GC=F").spec_sha256)
        self.assertNotEqual(spec.spec_sha256, dataclasses.replace(spec, gap_stress_pct=9.0).spec_sha256)
        with self.assertRaises(ValueError):
            load_instrument("DOGE")

    def test_invalid_specs_rejected(self):
        with self.assertRaises(ValueError):
            dataclasses.replace(GOLD, max_leverage=50.0)        # above assumed broker cap
        with self.assertRaises(ValueError):
            dataclasses.replace(GOLD, quantity_step=0)

    def test_bars_per_year_by_instrument(self):
        self.assertEqual(EUR.bars_per_year("1h"), 260 * 24)
        self.assertEqual(GOLD.bars_per_year("1d"), 252)
        self.assertEqual(SP.bars_per_year("5m"), 252 * 23 * 12)

    def test_sharpe_annualization_uses_instrument(self):
        bars = random_walk(300, seed=2)
        a = Simulator(SimConfig(instrument=GOLD)).run(bars, build_strategy("sma_cross", {"short": 5, "long": 20}))
        b = Simulator(SimConfig(instrument=dataclasses.replace(GOLD, trading_days_per_year=100))).run(
            bars, build_strategy("sma_cross", {"short": 5, "long": 20}))
        ma, mb = compute_metrics(a, "1d"), compute_metrics(b, "1d")
        self.assertAlmostEqual(abs(ma["sharpe_annualized"]) * (100 / 252) ** 0.5, abs(mb["sharpe_annualized"]), places=6)

    def test_instrument_costs_are_used(self):
        res = Simulator(SimConfig(instrument=EUR)).run(make_bars([(1.1, 1.1, 1.1, 1.1)] * 5), Scripted({0: Decision("BUY", stop_loss_pct=1.0)}))
        e = res.orders[0]
        self.assertAlmostEqual(e["fill_price"], 1.1 * (1 + (0.8 / 2 + 0.5) / 10_000))

    def test_same_strategy_runs_on_every_instrument(self):
        for spec in available_instruments().values():
            bars = random_walk(300, seed=4)
            res = Simulator(SimConfig(1000, instrument=spec, max_exposure=None)).run(
                bars, build_strategy("breakout", {"window": 10, "stop_loss_pct": max(1.0, spec.min_stop_pct * 2)}))
            m = compute_metrics(res, "1d")
            self.assertGreater(m["trades"], 3, spec.id)
            self.assertEqual(m["shortfall"], 0.0)
            for t in res.trades:       # sizes respect the instrument's step
                self.assertAlmostEqual(t["quantity"] / spec.quantity_step, round(t["quantity"] / spec.quantity_step), places=6)


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class MandatoryStopTests(unittest.TestCase):
    def test_strategies_refuse_missing_stop(self):
        for name in ("sma_cross", "breakout", "mean_reversion", "random_control"):
            with self.assertRaises(ValueError):
                build_strategy(name, {"stop_loss_pct": None})
            with self.assertRaises(ValueError):
                build_strategy(name, {"stop_loss_pct": 0})
            self.assertEqual(build_strategy(name).params()["stop_loss_pct"], 2.0)

    def test_search_never_samples_without_stop(self):
        for exp in plan_experiments(200, 1):
            self.assertIsNotNone(exp["params"]["stop_loss_pct"])

    def test_no_switch_to_disable_stop(self):
        self.assertFalse(any("stop" in f.name for f in dataclasses.fields(RiskProfile)))
        self.assertFalse(any("stop" in f.name and "require" in f.name for f in dataclasses.fields(SimConfig)))

    def test_entry_without_stop_is_rejected_by_execution_layer(self):
        bars = make_bars([(100, 100, 100, 100)] * 5)
        res = Simulator(SimConfig(1000, FREE, risk=LOOSE)).run(bars, Scripted({0: Decision("BUY")}))
        self.assertEqual(res.trades, [])
        self.assertEqual(res.orders[0]["reason"], "stop_loss_required")

    def test_stop_too_tight_for_instrument_rejected(self):
        bars = make_bars([(100, 100, 100, 100)] * 5)
        res = Simulator(SimConfig(1000, instrument=GOLD)).run(bars, Scripted({0: Decision("BUY", stop_loss_pct=0.01)}))
        self.assertEqual(res.orders[0]["reason"], "stop_too_tight")


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class SizingTests(unittest.TestCase):
    def sized(self, **kw):
        base = dict(equity=1000, fill_price=100, stop_pct=2.0, size_fraction=1.0, leverage_cap=10, fee_bps=0,
                    profile=RiskProfile(), spec=None)
        base.update(kw)
        return size_position(**base)

    def test_risk_limit_binds_and_loss_at_stop_equals_risk(self):
        d, why = self.sized()
        self.assertIsNone(why)
        self.assertEqual(d["binding_limit"], "risk")
        self.assertAlmostEqual(d["quantity"], 1000 * 0.01 / 2.0)           # 1 % of equity / $2 stop distance
        self.assertAlmostEqual(d["risk_amount"], 10.0)

    def test_leverage_limit_binds(self):
        d, _ = self.sized(leverage_cap=1.0, stop_pct=0.5, profile=RiskProfile(risk_per_trade_pct=50, max_worst_case_loss_pct=100))
        self.assertEqual(d["binding_limit"], "leverage")
        self.assertAlmostEqual(d["quantity"] * 100, 1000.0)

    def test_gap_stress_limit_binds_for_tight_stop(self):
        d, _ = self.sized(stop_pct=0.2, spec=GOLD, leverage_cap=5.0, profile=RiskProfile(risk_per_trade_pct=5, max_worst_case_loss_pct=2))
        self.assertEqual(d["binding_limit"], "gap_stress")
        self.assertLessEqual(d["worst_case_loss"], 1000 * 0.02 + 1e-9)

    def test_min_quantity_and_step(self):
        d, why = self.sized(equity=1.0, spec=GOLD, fill_price=2000.0)
        self.assertIsNone(d)
        self.assertEqual(why, "below_min_quantity")
        d, _ = self.sized(equity=100_000, spec=GOLD, fill_price=2000.0)
        self.assertAlmostEqual(d["quantity"] / 0.01, round(d["quantity"] / 0.01), places=6)

    def test_profile_validation(self):
        for kw in ({"risk_per_trade_pct": 0}, {"daily_loss_limit_pct": 101}, {"max_leverage": -1}, {"max_consecutive_losses": 0}):
            with self.assertRaises(ValueError):
                RiskProfile(**kw)

    def test_leverage_cap_resolution(self):
        self.assertEqual(SimConfig().leverage_cap(), 1.0)
        self.assertEqual(SimConfig(max_exposure=None, instrument=GOLD).leverage_cap(), 5.0)
        self.assertEqual(SimConfig(max_exposure=3.0, instrument=GOLD).leverage_cap(), 3.0)
        self.assertEqual(SimConfig(max_exposure=None, instrument=EUR, risk=RiskProfile(max_leverage=4.0)).leverage_cap(), 4.0)
        self.assertEqual(SimConfig(max_exposure=None).leverage_cap(), 1.0)

    def test_simulated_loss_at_stop_matches_one_percent_risk(self):
        bars = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 98, 99), (99, 99, 99, 99)])
        cfg = SimConfig(1000, FREE, max_exposure=10.0)
        res = Simulator(cfg).run(bars, Scripted({0: Decision("BUY", stop_loss_pct=2.0)}))
        t = res.trades[0]
        self.assertEqual(t["exit_reason"], "stop_loss")
        self.assertAlmostEqual(t["net_pnl"], -10.0, places=6)
        self.assertAlmostEqual(t["r_multiple"], -1.0, places=6)


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class GapSlippageFailureTests(unittest.TestCase):
    def run_gap(self, gap_to, spec=GOLD, risk=None, **kw):
        bars = make_bars([(2000, 2000, 2000, 2000), (2000, 2000, 2000, 2000),
                          (gap_to, gap_to, gap_to * 0.99, gap_to), (gap_to,) * 4])
        return Simulator(SimConfig(1000, FREE, max_exposure=None, instrument=spec, risk=risk or RiskProfile(), **kw)).run(
            bars, Scripted({0: Decision("BUY", stop_loss_pct=1.0)}))

    def test_gap_past_stop_fills_at_gap_and_is_bounded_by_gap_stress_sizing(self):
        res = self.run_gap(2000 * 0.97)          # a 3 % gap = the instrument's gap stress
        t = res.trades[0]
        self.assertEqual(t["exit_reason"], "stop_gap")
        self.assertLess(t["r_multiple"], -1.0)                      # worse than planned
        self.assertGreaterEqual(1000 + t["net_pnl"], 1000 * (1 - 0.05) - 1e-6)   # <= 5 % of equity
        self.assertEqual(res.shortfall, 0.0)

    def test_extreme_gap_with_governed_sizing_never_creates_debt(self):
        for gap in (0.9, 0.7, 0.5):
            self.assertEqual(self.run_gap(2000 * gap).shortfall, 0.0)

    def test_loose_profile_with_leverage_detects_debt(self):
        res = self.run_gap(2000 * 0.7, risk=LOOSE, stress=StressModel())
        # leverage cap 5 x 30 % gap = 150 % of equity lost -> negative balance is recorded, not hidden
        self.assertGreater(res.shortfall, 0.0)
        self.assertTrue(any(e["type"] == "negative_balance" for e in res.risk_events))
        self.assertEqual(compute_metrics(res)["shortfall"], res.shortfall)

    def test_stop_slippage_stress_worsens_exit(self):
        bars = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 97, 98), (98, 98, 98, 98)])
        base = Simulator(SimConfig(1000, FREE, risk=LOOSE)).run(bars, Scripted({0: Decision("BUY", stop_loss_pct=2.0)}))
        stressed = Simulator(SimConfig(1000, FREE, risk=LOOSE, stress=StressModel(stop_slippage_pct=1.0))).run(
            bars, Scripted({0: Decision("BUY", stop_loss_pct=2.0)}))
        self.assertAlmostEqual(base.trades[0]["exit_price"], 98.0)
        self.assertAlmostEqual(stressed.trades[0]["exit_price"], 98.0 * 0.99)

    def test_cost_multipliers_widen_costs(self):
        bars = make_bars([(100, 100, 100, 100)] * 5)
        costs = CostModel(0, 10, 10)
        a = Simulator(SimConfig(1000, costs, risk=LOOSE)).run(bars, Scripted({0: Decision("BUY", stop_loss_pct=50.0)}))
        b = Simulator(SimConfig(1000, costs, risk=LOOSE, stress=StressModel(spread_multiplier=3, slippage_multiplier=2))).run(
            bars, Scripted({0: Decision("BUY", stop_loss_pct=50.0)}))
        self.assertAlmostEqual(a.orders[0]["fill_price"], 100 * (1 + 15 / 10_000))
        self.assertAlmostEqual(b.orders[0]["fill_price"], 100 * (1 + (15 + 20) / 10_000))

    def test_stop_cannot_execute_fills_at_bar_extreme(self):
        bars = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 90, 95), (95, 95, 95, 95)])
        mk = lambda p: Simulator(SimConfig(1000, FREE, risk=LOOSE, stress=StressModel(stop_failure_prob=p, stop_failure_extra_pct=1.0))).run(
            bars, Scripted({0: Decision("BUY", stop_loss_pct=2.0)}))
        ok, failed = mk(0.0), mk(1.0)
        self.assertEqual(ok.trades[0]["exit_reason"], "stop_loss")
        t = failed.trades[0]
        self.assertEqual(t["exit_reason"], "stop_failed")
        self.assertAlmostEqual(t["exit_price"], 90 * 0.99)
        self.assertLess(t["net_pnl"], ok.trades[0]["net_pnl"])
        self.assertEqual(compute_metrics(failed)["stop_failures"], 1)

    def test_stop_failure_is_deterministic_per_seed(self):
        bars = random_walk(300, seed=8)
        def run(seed):
            return Simulator(SimConfig(1000, instrument=SP, max_exposure=None, seed=seed,
                                       stress=StressModel(stop_failure_prob=0.5))).run(bars, build_strategy("breakout", {"window": 8}))
        a, b, c = run(1), run(1), run(2)
        self.assertEqual(a.trades, b.trades)
        self.assertNotEqual([t["exit_reason"] for t in a.trades], [t["exit_reason"] for t in c.trades])

    def test_liquidation_level_triggers_before_far_stop(self):
        # 100 % worst-case loss allowed + a 50 % stop => 2x notional; the stop-out level (about -48 %) is hit before the stop
        spec = dataclasses.replace(GOLD, maintenance_margin_fraction=0.02)
        bars = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 50, 60), (60, 60, 60, 60)])
        risk = RiskProfile(risk_per_trade_pct=100, max_worst_case_loss_pct=100, daily_loss_limit_pct=100, max_drawdown_halt_pct=100)
        res = Simulator(SimConfig(1000, FREE, max_exposure=None, instrument=spec, risk=risk)).run(
            bars, Scripted({0: Decision("BUY", stop_loss_pct=50.0)}))
        t = res.trades[0]
        self.assertEqual(t["exit_reason"], "liquidation")
        self.assertGreater(t["exit_price"], 50.0)          # stop-out happens before the stop level
        self.assertAlmostEqual(t["exit_price"], 52.0, places=6)
        self.assertEqual(res.shortfall, 0.0)
        self.assertEqual(compute_metrics(res)["liquidations"], 1)

    def test_governed_sizing_keeps_stop_inside_liquidation_distance(self):
        # default profile: 1 % risk and a 2 % stop => liquidation level is far beyond the stop
        bars = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 97, 98), (98, 98, 98, 98)])
        res = Simulator(SimConfig(1000, FREE, max_exposure=None, instrument=GOLD)).run(
            bars, Scripted({0: Decision("BUY", stop_loss_pct=2.0)}))
        self.assertEqual(res.trades[0]["exit_reason"], "stop_loss")
        entry = [o for o in res.orders if o["kind"] == "entry"][0]
        self.assertTrue(entry["liquidation_price"] is None or entry["liquidation_price"] < entry["stop"])

    def test_insufficient_margin_rejected(self):
        spec = dataclasses.replace(GOLD, max_leverage=50.0, broker_max_leverage_assumed=60.0, maintenance_margin_fraction=0.05)
        bars = make_bars([(100, 100, 100, 100)] * 5)
        risk = RiskProfile(risk_per_trade_pct=100, max_worst_case_loss_pct=100)
        res = Simulator(SimConfig(1000, FREE, max_exposure=None, instrument=spec, risk=risk)).run(
            bars, Scripted({0: Decision("BUY", stop_loss_pct=1.0)}))      # 33x notional vs 5 % maintenance
        self.assertEqual(res.orders[0]["reason"], "insufficient_margin")


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class GovernorTests(unittest.TestCase):
    def test_daily_loss_limit_halts_entries_and_resets_next_day(self):
        rows = [(100, 100, 97, 100)] * 60            # every entry is stopped out at 99
        res = Simulator(SimConfig(1000, FREE, max_exposure=5.0,
                                  risk=RiskProfile(risk_per_trade_pct=2, daily_loss_limit_pct=3, max_drawdown_halt_pct=100,
                                                   max_consecutive_losses=1000))).run(hourly(rows), AlwaysBuy(1.0))
        halts = [e for e in res.risk_events if e["type"] == "daily_loss_halt"]
        self.assertGreaterEqual(len(halts), 2)                                   # day 1 and day 2/3
        rej = [o for o in res.orders if o["kind"] == "rejected" and o["reason"] == "risk_halt_daily"]
        self.assertTrue(rej)
        day = lambda ts: ts.date()
        day1 = [t for t in res.trades if day(t["entry_ts"]) == day(res.equity.index[0])]
        self.assertEqual(len(day1), 2)                                           # 2 x 2 % > 3 % -> stop trading
        later = [t for t in res.trades if day(t["entry_ts"]) > day(res.equity.index[0])]
        self.assertTrue(later)                                                   # trading resumes next day
        self.assertTrue(any(e["type"] == "daily_halt_lifted" for e in res.risk_events))

    def test_drawdown_halt_is_permanent_and_reported(self):
        rows = [(100, 100, 97, 100)] * 80
        res = Simulator(SimConfig(1000, FREE, max_exposure=10.0,
                                  risk=RiskProfile(risk_per_trade_pct=5, daily_loss_limit_pct=100, max_drawdown_halt_pct=12,
                                                   max_consecutive_losses=1000))).run(hourly(rows), AlwaysBuy(1.0))
        self.assertEqual(res.halted, "max_drawdown_halt")
        self.assertTrue(any(e["type"] == "max_drawdown_halt" for e in res.risk_events))
        last_entry = max(t["entry_ts"] for t in res.trades)
        self.assertLess(last_entry, res.equity.index[-1])
        self.assertTrue(any(o["reason"] == "risk_halt_drawdown" for o in res.orders if o["kind"] == "rejected"))
        self.assertGreaterEqual(res.equity.equity.min(), 1000 * 0.80)             # halt limits the damage

    def test_consecutive_loss_limit(self):
        rows = [(100, 100, 97, 100)] * 20
        res = Simulator(SimConfig(1000, FREE, max_exposure=5.0,
                                  risk=RiskProfile(risk_per_trade_pct=0.1, daily_loss_limit_pct=100, max_drawdown_halt_pct=100,
                                                   max_consecutive_losses=3))).run(hourly(rows), AlwaysBuy(1.0))
        self.assertEqual(len(res.trades), 3)
        self.assertTrue(any(o["reason"] == "risk_halt_loss_streak" for o in res.orders if o["kind"] == "rejected"))


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class NeverIndebtTests(unittest.TestCase):
    def test_gap_injection_is_deterministic_and_valid(self):
        bars = random_walk(200, seed=1)
        a, b = inject_gaps(bars, probability=0.05, size_pct=3, seed=5), inject_gaps(bars, probability=0.05, size_pct=3, seed=5)
        pd.testing.assert_frame_equal(a, b)
        self.assertFalse(a.equals(bars))
        self.assertTrue((a["High"] >= a[["Open", "Close", "Low"]].max(axis=1) - 1e-9).all())
        pd.testing.assert_frame_equal(inject_gaps(bars, probability=0, size_pct=3), bars)
        with self.assertRaises(ValueError):
            inject_gaps(bars, probability=2, size_pct=3)

    def test_no_debt_under_combined_stress_across_instruments_and_strategies(self):
        stress = StressModel(spread_multiplier=3, slippage_multiplier=3, stop_slippage_pct=0.5,
                             stop_failure_prob=0.3, stop_failure_extra_pct=0.5)
        strategies = [("sma_cross", {"short": 5, "long": 20}), ("breakout", {"window": 10}),
                      ("mean_reversion", {"window": 20, "z_entry": 1.5}), ("random_control", {"seed": 4, "trade_prob": 0.1})]
        runs = trades = 0
        for spec in available_instruments().values():
            for seed in (1, 2, 3):
                bars = inject_gaps(random_walk(400, seed=seed), probability=0.02, size_pct=spec.gap_stress_pct, seed=seed)
                for name, params in strategies:
                    res = Simulator(SimConfig(1000, instrument=spec, max_exposure=None, stress=stress, seed=seed)).run(
                        bars, build_strategy(name, {**params, "stop_loss_pct": max(1.0, spec.min_stop_pct * 2)}))
                    runs += 1
                    trades += len(res.trades)
                    self.assertEqual(res.shortfall, 0.0, (spec.id, seed, name))
                    self.assertGreater(res.equity.equity.min(), 0.0)
                    for t in res.trades:      # no single trade may lose more than 40 % of the starting account
                        self.assertGreater(t["net_pnl"], -400.0, (spec.id, seed, name, t["exit_reason"]))
        self.assertGreater(trades, 100)


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class ExperimentInstrumentTests(unittest.TestCase):
    def test_instrument_spec_travels_into_registry_and_final_test(self):
        import tempfile
        from pathlib import Path
        from market_data.laboratory import ExperimentRegistry
        from market_data.academy.experiments import run_training, run_final_test
        with tempfile.TemporaryDirectory() as tmp:
            reg = ExperimentRegistry(Path(tmp) / "r.db")
            bars = random_walk(700, seed=21)
            cfg = SimConfig(1000, instrument=SP, max_exposure=None, stress=StressModel(spread_multiplier=2))
            out = run_training(bars, reg, symbol="ES=F", interval="1d", experiments=16, seed=2, sim_config=cfg, control_runs=2)
            rec = reg.recent(100)[-1]
            self.assertEqual(rec["configuration"]["instrument_id"], "SP500")
            self.assertEqual(rec["configuration"]["instrument_spec_sha256"], SP.spec_sha256)
            self.assertEqual(SimConfig.from_dict(rec["configuration"]["sim_config"]).instrument, SP)
            if out["selected_experiment_id"] is None:
                self.skipTest("no experiment met the minimum trade count")
            res = run_final_test(bars, reg, out["selected_experiment_id"])
            self.assertEqual(res["final_test"]["shortfall"], 0.0)
            reg.close()

    def test_simconfig_roundtrip(self):
        cfg = SimConfig(500, None, None, 2.0, False, GOLD, RiskProfile(2, 3, 4, 5, 6, 7), StressModel(2, 3, 0.1, 0.2, 0.3), 9)
        self.assertEqual(SimConfig.from_dict(cfg.to_dict()).to_dict(), cfg.to_dict())


if __name__ == "__main__":
    unittest.main()
