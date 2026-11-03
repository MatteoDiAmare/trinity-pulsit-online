"""Intraday: sessions (24/7, 24/5, Globex break, regular hours), Saba's intraday-only policy inside the simulator, time-of-day costs and financing,
days-based folds with timestamp pooling, annualisation, the Bitstamp client - and that every daily result stays exactly what it was."""
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
    from market_data.archive import MarketArchive
    from market_data.bitstamp import BitstampProvider, STEPS
    from market_data.laboratory import ExperimentRegistry
    from market_data.academy.instruments import load_instrument
    from market_data.academy.intraday import IntradayCosts, SessionRules, intraday_costs, load_intraday_profiles, session_kind
    from market_data.academy.sessions import KINDS, check_session, expected_index, in_session
    from market_data.academy.simulator import SimConfig, Simulator
    from market_data.academy.strategies import Decision
    from market_data.academy.study import StudySpec, _time_folds, run_recorded_study, run_study
    from tests.test_academy_study import bars_from_returns
except ImportError:
    pd = None


def hourly(start="2024-01-01", days=60, tz="UTC", kind="24x7", seed=1, vol=0.003, drift=0.0, base=100.0, interval="60m"):
    """Synthetic OHLC bars on the exact expected index of a session kind."""
    idx = expected_index(kind, pd.Timestamp(start, tz="UTC"), pd.Timestamp(start, tz="UTC") + pd.Timedelta(days=days), interval)
    r = np.random.default_rng(seed).normal(drift, vol, len(idx))
    close = base * np.cumprod(1 + r)
    open_ = np.concatenate([[base], close[:-1]])
    df = pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) * 1.001, "Low": np.minimum(open_, close) * 0.999, "Close": close, "Volume": 100.0}, index=idx)
    return df


class Scripted:
    """Strategy that follows a script {bar_count: Decision} - exact control for simulator tests."""
    def __init__(self, script):
        self.script = script

    def on_bar(self, view, position):
        return self.script.get(view.total, Decision("ABSTAIN"))


def sim(bars, strategy, **kw):
    cfg = SimConfig(1000.0, None, None, instrument=kw.pop("instrument", load_instrument("BTCUSD")), **kw)
    return Simulator(cfg).run(bars, strategy)


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class SessionCalendarTests(unittest.TestCase):
    def test_expected_bar_counts_and_closures(self):
        e247 = expected_index("24x7", "2026-03-02", "2026-03-15 23:00", "60m")
        self.assertEqual(len(e247), 14 * 24)
        fx = expected_index("fx_24x5", "2026-03-02", "2026-03-15 23:00", "60m")
        self.assertLess(len(fx), len(e247))
        local = fx.tz_convert("America/New_York")
        self.assertFalse(((local.dayofweek == 5)).any())                                   # nothing on Saturday
        self.assertFalse(((local.dayofweek == 4) & (local.hour >= 17)).any())             # Friday closes 17:00 New York
        self.assertTrue(((local.dayofweek == 6) & (local.hour >= 17)).any())              # Sunday opens 17:00 New York
        gx = expected_index("cme_globex", "2026-03-02", "2026-03-06 23:00", "60m").tz_convert("America/New_York")
        mon = sorted({t.hour for t in gx if t.strftime("%Y-%m-%d") == "2026-03-03"})
        self.assertNotIn(17, mon)                                                          # the daily break 17:00-18:00
        self.assertEqual(len(mon), 23)
        rth = expected_index("rth_us", "2026-03-02", "2026-03-06 23:00", "30m").tz_convert("America/New_York")
        self.assertEqual(len(rth), 5 * 13)                                                 # 09:30..15:30 = 13 half-hours
        self.assertEqual((rth[0].hour, rth[0].minute, rth[12].hour, rth[12].minute), (9, 30, 15, 30))

    def test_daylight_saving_shift_is_followed(self):
        winter = expected_index("rth_us", "2026-03-02", "2026-03-02 23:00", "60m")[0]
        summer = expected_index("rth_us", "2026-03-10", "2026-03-10 23:00", "60m")[0]        # US DST began 2026-03-08
        self.assertEqual((winter.hour, summer.hour), (14, 13))

    def test_unknown_kind_and_interval(self):
        with self.assertRaises(ValueError):
            expected_index("moon", "2026-01-01", "2026-01-02", "60m")
        with self.assertRaises(ValueError):
            expected_index("24x7", "2026-01-01", "2026-01-02", "1d")

    def test_check_session_pass_missing_and_outside(self):
        b = hourly(days=30)
        self.assertEqual(check_session(b, "24x7", "60m")["status"], "pass")
        gap = b.drop(b.index[100:130])                                                      # 30 consecutive hours missing
        r = check_session(gap, "24x7", "60m")
        self.assertEqual((r["status"], r["missing_expected_bars"], r["longest_missing_run_bars"]), ("warn", 30, 30))
        weekend_ok = hourly(kind="fx_24x5", days=30)                                        # a 24/5 series is complete although it has no weekend
        self.assertEqual(check_session(weekend_ok, "fx_24x5", "60m")["status"], "pass")
        self.assertEqual(check_session(weekend_ok, "24x7", "60m")["status"], "fail")        # ...but is NOT a complete 24/7 series (about 70 % of the bars)
        wrong = check_session(hourly(kind="24x7", days=20), "rth_us", "60m")                # round-the-clock data labelled regular-hours
        self.assertIn(wrong["status"], ("warn", "fail"))
        self.assertGreater(wrong["outside_session_bars"], 0)
        half = b.iloc[::2]
        self.assertEqual(check_session(half, "24x7", "60m")["status"], "fail")
        self.assertEqual(check_session(b.iloc[:0], "24x7", "60m")["status"], "fail")

    def test_providers_that_close_an_hour_off_the_venue_clock_are_not_called_broken(self):
        # Yahoo closes spot FX at a fixed UTC time while the New York convention moves with DST: one hour either side of a boundary is tolerated and counted
        fx = hourly(kind="fx_24x5", days=60)
        loc = fx.index.tz_convert("America/New_York")
        shifted = fx[~((loc.dayofweek == 6) & (loc.hour == 17))]                           # Sunday's first hour missing
        extra = fx.iloc[[0]].copy()
        extra.index = [fx.index[(loc.dayofweek == 4) & (loc.hour == 16)][3] + pd.Timedelta(hours=1)]       # one bar after the Friday close
        r = check_session(pd.concat([shifted, extra]).sort_index(), "fx_24x5", "60m")
        self.assertEqual(r["status"], "pass", r["findings"])
        self.assertGreater(r["boundary_bars"], 0)
        self.assertEqual((r["missing_expected_bars"], r["outside_session_bars"]), (0, 0))
        far = fx.iloc[[0]].copy()
        far.index = [fx.index[(loc.dayofweek == 4) & (loc.hour == 16)][3] + pd.Timedelta(hours=30)]         # a Saturday bar is not boundary noise
        self.assertGreater(check_session(pd.concat([fx, far]).sort_index(), "fx_24x5", "60m")["outside_session_bars"], 0)
        strict = check_session(pd.concat([shifted, extra]).sort_index(), "fx_24x5", "60m", boundary_tolerance_min=0)
        self.assertGreater(strict["missing_expected_bars"] + strict["outside_session_bars"], 0)

    def test_in_session_mask(self):
        b = hourly(kind="rth_us", days=10)
        self.assertTrue(in_session(b.index, "rth_us", "60m").all())


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class PolicyAndCostTests(unittest.TestCase):
    def test_session_rules_follow_the_real_policy_and_the_local_clock(self):
        from market_data.intraday_policy import IntradayPolicy
        r = SessionRules.from_policy(60)
        p = IntradayPolicy()
        self.assertEqual((r.last_entry_min, r.flatten_min), (p.last_entry.hour * 60 + p.last_entry.minute, p.flatten_by.hour * 60 + p.flatten_by.minute))
        idx = pd.DatetimeIndex(["2026-01-15 19:00", "2026-01-15 20:00", "2026-01-15 21:00", "2026-07-15 18:00", "2026-07-15 19:00"], tz="UTC")     # CET = UTC+1, CEST = UTC+2
        a = r.arrays(idx)
        # winter: 19:00 UTC = 20:00 local -> bar ends 21:00 -> entry ok;  20:00 UTC = 21:00 local -> ends 22:00 > 21:45 -> flat, no entry
        self.assertEqual(list(a["entry_ok"]), [True, False, False, True, False])
        self.assertEqual(list(a["must_flat"]), [False, True, True, False, True])
        r15 = SessionRules.from_policy(15)
        b = r15.arrays(pd.DatetimeIndex(["2026-01-15 20:15", "2026-01-15 20:30", "2026-01-15 20:45"], tz="UTC"))                            # 21:15, 21:30, 21:45 local
        self.assertEqual(list(b["entry_ok"]), [True, False, False])         # no entry from 21:30
        self.assertEqual(list(b["must_flat"]), [False, False, True])        # a 21:30 bar ends exactly at 21:45 (allowed); 21:45 must be flat
        with self.assertRaises(ValueError):
            SessionRules(60, last_entry_min=1300, flatten_min=1200)

    def test_default_has_no_session_and_no_new_keys(self):
        cfg = SimConfig(1000.0, None, None, instrument=load_instrument("BTCUSD"))
        self.assertNotIn("session", cfg.to_dict())
        self.assertNotIn("intraday_costs", cfg.to_dict())
        b = hourly(days=20)
        res = sim(b, Scripted({10: Decision("BUY", stop_loss_pct=2.0), 30: Decision("CLOSE")}))
        self.assertTrue(res.trades)
        self.assertNotIn("financing", res.trades[0])                         # trade records keep their old shape for daily studies
        self.assertEqual(SimConfig.from_dict(cfg.to_dict()).to_dict(), cfg.to_dict())

    def test_config_roundtrip_with_session_and_costs(self):
        cfg = SimConfig(1000.0, None, None, instrument=load_instrument("BTCUSD"), session=SessionRules.from_policy(60), intraday_costs=intraday_costs("BTCUSD"))
        back = SimConfig.from_dict(cfg.to_dict())
        self.assertEqual(back.to_dict(), cfg.to_dict())
        self.assertEqual((back.session, back.intraday_costs), (cfg.session, cfg.intraday_costs))

    def test_policy_closes_before_the_cutoff_and_blocks_late_entries(self):
        b = hourly(start="2026-01-12", days=10, vol=0.0005)                  # winter: flat by 20:45 UTC
        always = Scripted({k: Decision("BUY", stop_loss_pct=5.0) for k in range(1, len(b))})
        carried = sim(b, always)
        self.assertTrue(any(t["entry_ts"].date() != t["exit_ts"].date() for t in carried.trades), "without the policy a long position is carried overnight")
        pol = sim(b, always, session=SessionRules.from_policy(60))
        self.assertTrue(pol.trades)
        loc = lambda ts: ts.tz_convert("Europe/Stockholm")  # noqa: E731
        for t in pol.trades:
            self.assertEqual(loc(t["entry_ts"]).date(), loc(t["exit_ts"]).date(), "a trade crossed local midnight")
            self.assertLessEqual(loc(t["exit_ts"]).hour * 60 + loc(t["exit_ts"]).minute, 21 * 60 + 45)
        self.assertIn("policy_flatten", {t["exit_reason"] for t in pol.trades})
        rejected = {o["reason"] for o in pol.orders if o["kind"] == "rejected"}
        self.assertIn("policy_no_entry", rejected)
        # no bar in which the position is held starts at or after the flatten time
        eq = pol.equity
        held = eq[eq["position_side"] != 0].index.tz_convert("Europe/Stockholm")
        self.assertFalse(((held.hour * 60 + held.minute) > 21 * 60 + 45).any())

    def test_limited_hours_market_is_flat_at_the_last_close_not_carried_to_tomorrow(self):
        rth = hourly(start="2026-01-12", days=14, kind="rth_us", vol=0.0005, base=4000.0)             # 09:30-16:00 New York = 15:30-22:00 Stockholm in winter
        always = Scripted({k: Decision("BUY", stop_loss_pct=5.0) for k in range(1, len(rth))})
        spx = load_instrument("SP500")
        free = sim(rth, always, instrument=spx)
        self.assertTrue(any(t["entry_ts"].date() != t["exit_ts"].date() for t in free.trades), "with no policy the position is carried overnight")
        pol = sim(rth, always, instrument=spx, session=SessionRules.from_policy(60))
        loc = lambda ts: ts.tz_convert("Europe/Stockholm")  # noqa: E731
        self.assertTrue(pol.trades)
        for t in pol.trades:
            self.assertEqual(loc(t["entry_ts"]).date(), loc(t["exit_ts"]).date())
        self.assertIn("policy_flatten", {t["exit_reason"] for t in pol.trades})
        # the daily bars of the cash session: with a policy every position is closed at the close of the last bar of the day
        last_bars = {ts for ts in rth.index if ts == rth.index[rth.index.normalize() == ts.normalize()][-1]}
        flat_exits = [t for t in pol.trades if t["exit_reason"] == "policy_flatten"]
        self.assertTrue(all(t["exit_ts"] in last_bars for t in flat_exits))

    def test_daily_loss_limit_resets_at_local_midnight(self):
        r = SessionRules.from_policy(60)
        idx = pd.DatetimeIndex(["2026-01-15 22:30", "2026-01-15 23:30", "2026-01-16 00:30"], tz="UTC")          # local 23:30, 00:30, 01:30
        d = r.arrays(idx)["day"]
        self.assertNotEqual(d[0], d[1])                                      # 23:30 local and 00:30 local are different local days
        self.assertEqual(d[1], d[2])

    def test_intraday_cost_arrays(self):
        c = IntradayCosts(spread_hour_mult=tuple([1.0] * 12 + [3.0] * 12), weekend_spread_mult=2.0, rollover_hour_utc=21, triple_weekday_utc=2,
                          skip_weekend_rollover=True, financing_long_bps_per_day=2.0)
        idx = pd.date_range("2026-03-02 00:00", "2026-03-09 23:00", freq="h", tz="UTC")                       # Mon..Mon
        a = c.arrays(idx)
        mult = dict(zip(idx, a["spread_mult"]))
        self.assertEqual(mult[pd.Timestamp("2026-03-03 05:00", tz="UTC")], 1.0)
        self.assertEqual(mult[pd.Timestamp("2026-03-03 15:00", tz="UTC")], 3.0)
        self.assertEqual(mult[pd.Timestamp("2026-03-07 15:00", tz="UTC")], 6.0)                              # Saturday afternoon: 3 x 2
        fin = pd.Series(a["fin_days"], index=idx)
        by_day = fin.groupby(fin.index.date).sum()
        self.assertEqual(by_day[pd.Timestamp("2026-03-03").date()], 1.0)       # Tuesday rollover = 1 day
        self.assertEqual(by_day[pd.Timestamp("2026-03-04").date()], 3.0)       # Wednesday = triple
        self.assertEqual(by_day[pd.Timestamp("2026-03-07").date()], 0.0)       # Saturday: closed, nothing charged
        self.assertEqual(by_day[pd.Timestamp("2026-03-08").date()], 0.0)       # Sunday
        # 24/7 market charges every day
        c2 = IntradayCosts(rollover_hour_utc=0, skip_weekend_rollover=False, financing_long_bps_per_day=3.0)
        f2 = pd.Series(c2.arrays(idx)["fin_days"], index=idx).groupby(idx.date).sum()
        self.assertEqual(f2[pd.Timestamp("2026-03-07").date()], 1.0)
        with self.assertRaises(ValueError):
            IntradayCosts(spread_hour_mult=(1.0,) * 23)
        with self.assertRaises(ValueError):
            IntradayCosts(rollover_hour_utc=24)

    def test_spread_depends_on_the_hour_slippage_on_the_range_and_stress_doubles_it(self):
        thin = IntradayCosts(spread_hour_mult=tuple([1.0] * 12 + [4.0] * 12), slippage_range_fraction=0.0)
        b = hourly(days=6, vol=0.0005)
        buy_at = lambda k: Scripted({k: Decision("BUY", stop_loss_pct=50.0), k + 3: Decision("CLOSE")})  # noqa: E731
        i_morning = int(np.where(b.index.hour == 4)[0][2])
        i_evening = int(np.where(b.index.hour == 16)[0][2])
        cheap = sim(b, buy_at(i_morning), intraday_costs=thin).trades[0]
        dear = sim(b, buy_at(i_evening), intraday_costs=thin).trades[0]
        per_unit = lambda t: t["spread_slippage_cost"] / t["quantity"] / t["entry_price"]  # noqa: E731
        # half-spread 2.5 bps x1 vs x4 plus a constant 5 bps slippage per fill: 7.5 vs 15 bps, i.e. twice as dear
        self.assertAlmostEqual(per_unit(dear) / per_unit(cheap), 2.0, delta=0.15)
        # slippage grows with the previous bar's range (known before the order: no look-ahead)
        calm, wild = hourly(days=6, vol=0.0005, seed=4), hourly(days=6, vol=0.0005, seed=4)
        k = int(np.where(wild.index.hour == 4)[0][2])
        wild.iloc[k - 1, wild.columns.get_loc("High")] = wild.iloc[k - 1]["Close"] * 1.03                  # the bar BEFORE the fill was violent
        slip = IntradayCosts(slippage_range_fraction=0.5)
        c1 = sim(calm, buy_at(k), intraday_costs=slip).trades[0]["spread_slippage_cost"]
        c2 = sim(wild, buy_at(k), intraday_costs=slip).trades[0]["spread_slippage_cost"]
        self.assertGreater(c2, 1.5 * c1)
        # a violent bar AFTER the order does not change the entry cost (no look-ahead)
        later = hourly(days=6, vol=0.0005, seed=4)
        later.iloc[k, later.columns.get_loc("High")] = later.iloc[k]["Close"] * 1.05
        self.assertAlmostEqual(sim(later, buy_at(k), intraday_costs=slip).trades[0]["entry_price"], sim(calm, buy_at(k), intraday_costs=slip).trades[0]["entry_price"], places=9)
        from market_data.academy.risk import StressModel
        base = sim(b, buy_at(i_evening), intraday_costs=thin).trades[0]["spread_slippage_cost"]
        x2 = sim(b, buy_at(i_evening), intraday_costs=thin, stress=StressModel(spread_multiplier=2.0, slippage_multiplier=2.0)).trades[0]["spread_slippage_cost"]
        self.assertGreater(x2, 1.9 * base)

    def test_financing_is_charged_only_when_carrying_across_the_rollover(self):
        fin = IntradayCosts(rollover_hour_utc=12, skip_weekend_rollover=False, financing_long_bps_per_day=10.0, financing_short_bps_per_day=-5.0)
        b = hourly(days=8, vol=0.0001)
        k0 = int(np.where(b.index.hour == 6)[0][1])                                    # enter 06:00, hold 3 rollovers (12:00 each day), exit
        script = lambda side: Scripted({k0: Decision(side, stop_loss_pct=50.0), k0 + 24 * 3: Decision("CLOSE")})  # noqa: E731
        long_t = sim(b, script("BUY"), intraday_costs=fin).trades[0]
        flat_t = sim(b, script("BUY"), intraday_costs=IntradayCosts()).trades[0]
        self.assertAlmostEqual(long_t["financing"], long_t["quantity"] * b["Close"].iloc[k0 : k0 + 72].mean() * 10 / 1e4 * 3, delta=long_t["financing"] * 0.05)
        self.assertAlmostEqual(flat_t["net_pnl"] - long_t["net_pnl"], long_t["financing"], places=6)                # net result includes the charge, once
        short_t = sim(b, script("SELL"), intraday_costs=fin).trades[0]
        self.assertLess(short_t["financing"], 0)                                          # negative rate = the short RECEIVES
        intra = sim(b, Scripted({k0: Decision("BUY", stop_loss_pct=50.0), k0 + 3: Decision("CLOSE")}), intraday_costs=fin).trades[0]
        self.assertEqual(intra["financing"], 0.0)                                         # closed before the 12:00 rollover: nothing

    def test_profiles_are_valid_documented_and_do_not_touch_the_instrument_specs(self):
        prof = load_intraday_profiles()
        for iid, p in prof.items():
            load_instrument(iid)                                                          # every profile belongs to a real instrument
            self.assertIn(p["session"], KINDS)
            self.assertEqual(intraday_costs(iid).to_dict()["spread_hour_mult"], p["costs"]["spread_hour_mult"])
            self.assertIn("assumption", p["costs"]["source"])
        self.assertEqual(session_kind("GOLD"), "cme_globex")
        self.assertEqual(session_kind("BTCUSD_BITSTAMP"), "24x7")
        self.assertIsNone(session_kind("NATGAS"))
        # the three original instrument specs are byte-identical to before (their hashes are part of old results)
        g = load_instrument("GOLD")
        self.assertEqual((g.costs.fee_bps, g.costs.spread_bps, g.costs.slippage_bps), (2.0, 3.0, 2.0))
        self.assertNotIn("intraday", json.dumps(g.to_dict()))


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class StudyIntradayTests(unittest.TestCase):
    GRID = {"short": [6, 12], "long": [24, 48]}

    def spec(self, **kw):
        base = dict(name="i", strategy="sma_cross", base_params={"stop_loss_pct": 1.5}, grid=self.GRID, instruments=["BTCUSD", "GOLD"], interval="60m",
                    fold_mode="days", train_days=40, test_days=10, respect_usable_from=False)
        base.update(kw)
        return StudySpec(**base)

    def test_validation_and_explicit_policy(self):
        s = self.spec()
        self.assertIs(s.allow_overnight, False)                                             # the policy is explicit in the recorded spec
        self.assertEqual(s.to_dict()["allow_overnight"], False)
        self.assertIs(self.spec(allow_overnight=True).allow_overnight, True)
        for bad in (dict(fold_mode="dates"), dict(fold_mode="bars"), dict(pooling="fixed"), dict(train_days=None), dict(test_days=0), dict(step_days=5, test_days=10)):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.spec(**bad)
        with self.assertRaises(ValueError):
            StudySpec(name="d", strategy="sma_cross", base_params={"stop_loss_pct": 2.0}, grid=self.GRID, instruments=["GOLD"], train_days=100, test_days=20)   # days for a daily series

    def test_daily_study_records_and_hashes_are_unchanged(self):
        kw = dict(name="sma grid", strategy="sma_cross", base_params={"stop_loss_pct": 2.0}, grid={"short": [5, 10, 20], "long": [40, 80, 150]},
                  instruments=["GOLD", "EURUSD", "SP500"], interval="1d", train_bars=750, test_bars=125)
        s = StudySpec(**kw)
        d = s.to_dict()
        for k in ("train_days", "test_days", "step_days", "allow_overnight", "intraday_costs"):
            self.assertNotIn(k, d)
        self.assertEqual(s.sha256, "a56a61551712570c" + s.sha256[16:])                        # the value produced before the intraday work existed

    def test_time_folds_use_calendar_days_and_never_lend_the_future(self):
        a = hourly(start="2024-01-01", days=200, seed=1)
        late = hourly(start="2024-03-01", days=140, seed=2)                               # starts two months later
        f = _time_folds({"A": a, "B": late}, self.spec(train_days=60, test_days=15))
        self.assertTrue(f["A"] and f["B"])
        self.assertLess(len(f["B"]), len(f["A"]))
        for i, d in (("A", a), ("B", late)):
            for fold in f[i]:
                tr_end, te_start, te_end = d.index[fold["train_end"] - 1], d.index[fold["test_start"]], d.index[min(fold["test_end"], len(d)) - 1]
                self.assertLess(tr_end, te_start)
                self.assertAlmostEqual((te_end - te_start).days, 15, delta=1)
        a_by = {x["index"]: x for x in f["A"]}
        for fb in f["B"]:                                                                 # B only takes part in windows its history covers, on the SAME calendar as A
            self.assertGreaterEqual(late.index[fb["train_start"]], pd.Timestamp("2024-03-01", tz="UTC"))
            self.assertEqual(a.index[a_by[fb["index"]]["test_start"]], a.index[a.index.searchsorted(late.index[fb["test_start"]])])

    def test_end_to_end_policy_costs_and_annualisation(self):
        bars = {"BTCUSD": hourly(start="2023-01-01", days=330, seed=3, drift=0.0004), "GOLD": hourly(start="2023-01-01", days=330, kind="cme_globex", seed=4, drift=0.0004)}
        dev_end = {i: len(b) for i, b in bars.items()}
        pol = run_study(self.spec(allow_overnight=False), bars, dev_end)
        self.assertEqual(pol["intraday"]["allow_overnight"], False)
        self.assertEqual(pol["wfo"]["trades_held_overnight"], 0)
        self.assertGreater(pol["wfo"]["trades"], 20)
        self.assertGreater(pol["wfo"]["exit_reasons"].get("policy_flatten", 0), 0)
        self.assertIn("assumption", pol["intraday"]["cost_profiles"]["BTCUSD"])
        self.assertEqual(pol["fold_mode"], "days")
        # annualisation comes from the data: a union of 24/7 and 23h/5d markets has more than 5796 and at most 8784 bars per year
        self.assertTrue(5800 < pol["bars_per_year_used"] < 8800, pol["bars_per_year_used"])
        over = run_study(self.spec(allow_overnight=True), bars, dev_end)
        self.assertEqual(over["intraday"]["allow_overnight"], True)
        self.assertGreater(over["wfo"]["trades_held_overnight"], 0, "when overnight is allowed explicitly, carried positions exist")
        x2 = run_study(self.spec(cost_multiplier=2.0), bars, dev_end)
        self.assertLess(x2["wfo"]["return_pct"], pol["wfo"]["return_pct"])                  # double costs cost money
        self.assertGreater(x2["wfo"]["spread_slippage_and_fees_paid"], 1.3 * pol["wfo"]["spread_slippage_and_fees_paid"])
        flat = run_study(self.spec(intraday_costs=False), bars, dev_end)
        self.assertIn("flat bps", flat["intraday"]["cost_profiles"]["BTCUSD"])

    def test_limited_hours_market_is_annualised_from_its_own_bars(self):
        rth = hourly(start="2023-01-01", days=330, kind="rth_us", seed=5, drift=0.0004, base=4000.0)
        r = run_study(self.spec(instruments=["SP500"]), {"SP500": rth}, {"SP500": len(rth)})
        per_year = len(rth) / ((rth.index[-1] - rth.index[0]).days / 365.25)
        self.assertLess(r["bars_per_year_used"], 2200)                                       # ~ 6 bars x 252 days; the instrument spec alone would say 5796
        self.assertAlmostEqual(r["bars_per_year_used"], per_year, delta=per_year * 0.2)
        self.assertEqual(r["wfo"]["trades_held_overnight"], 0)

    def test_a_study_in_which_every_cell_fails_is_refused_not_recorded(self):
        b = {"BTCUSD": hourly(days=200)}
        with self.assertRaisesRegex(ValueError, "every cell of the study failed"):
            run_study(self.spec(instruments=["BTCUSD"], grid={"short": [50], "long": [20]}), b, {"BTCUSD": len(b["BTCUSD"])})

    def test_recorded_study_checks_the_session_and_ignores_daily_usable_from(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = hourly(start="2023-01-01", days=300, seed=6)
            for sym, df in (("BTCUSD-BITSTAMP", good),):
                d = root / sym / "60m"
                d.mkdir(parents=True)
                df.assign(Dividends=0.0, **{"Stock Splits": 0.0}).to_parquet(d / "bars.parquet")
            reg = ExperimentRegistry(root / "reg.sqlite3")
            spec = self.spec(instruments=["BTCUSD_BITSTAMP"], respect_usable_from=True)
            out = run_recorded_study(reg, MarketArchive(root), spec)
            q = out["data_quality"]["BTCUSD_BITSTAMP"]
            self.assertEqual(q["session"]["status"], "pass")
            self.assertEqual(q["session"]["kind"], "24x7")
            again = run_recorded_study(reg, MarketArchive(root), spec)
            self.assertTrue(again["reused"])
            # data with half of the hours missing is refused with the session finding
            d2 = root / "BTCUSD-BITSTAMP" / "60m"
            good.iloc[::2].assign(Dividends=0.0, **{"Stock Splits": 0.0}).to_parquet(d2 / "bars.parquet")
            with self.assertRaisesRegex(ValueError, "session check failed"):
                run_recorded_study(reg, MarketArchive(root), self.spec(instruments=["BTCUSD_BITSTAMP"], name="other"))
            reg.close()


class FakeBitstamp:
    """Minimal stand-in for the public endpoint: serves candles for a fixed range, up to `limit` per call."""
    def __init__(self, first, last, step, fail_first=0):
        self.first, self.last, self.step, self.fail_first, self.calls = first, last, step, fail_first, []

    def __call__(self, url):
        self.calls.append(url)
        if self.fail_first:
            self.fail_first -= 1
            raise urllib.error.HTTPError(url, 429, "slow down", {}, None)
        q = dict(p.split("=") for p in url.split("?")[1].split("&"))
        start, limit, step = int(q["start"]), int(q["limit"]), int(q["step"])
        assert step == self.step and limit == 1000
        ts = [t for t in range(max(start, self.first), self.last + 1, step)][:limit]
        return json.dumps({"data": {"pair": "BTC/USD", "ohlc": [{"timestamp": str(t), "open": "100.0", "high": "101.0", "low": "99.0", "close": "100.5", "volume": "1.5"} for t in ts]}}).encode()


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class BitstampTests(unittest.TestCase):
    def provider(self, fake, **kw):
        self.sleeps = []
        return BitstampProvider("btcusd", start="2024-01-01", opener=fake, sleep=self.sleeps.append, now=pd.Timestamp("2024-03-01", tz="UTC"), **kw)

    def test_pagination_dedupe_forming_candle_and_gentle_rate(self):
        t0 = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp())
        fake = FakeBitstamp(t0, t0 + 3600 * 2400, 3600)                                       # 2401 hourly candles available
        now = pd.Timestamp(t0 + 3600 * 2400 + 1800, unit="s", tz="UTC")                      # the last candle is still forming at "now"
        p = BitstampProvider("btcusd", start="2024-01-01", opener=fake, sleep=lambda s: None, now=now)
        df = p.history(interval="60m")
        self.assertEqual(len(df), 2400)                                                       # forming candle dropped
        self.assertTrue(df.index.is_monotonic_increasing and not df.index.has_duplicates)
        self.assertEqual(df.index[0], pd.Timestamp("2024-01-01", tz="UTC"))
        self.assertEqual(len(fake.calls), 3)                                                  # 1000 + 1000 + 401
        self.assertTrue(all("step=3600" in c and "limit=1000" in c for c in fake.calls))
        self.assertEqual(p.requests, 3)

    def test_rate_limit_backoff_and_failure(self):
        t0 = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp())
        fake = FakeBitstamp(t0, t0 + 3600 * 10, 3600, fail_first=2)
        p = self.provider(fake)
        df = p.history(interval="60m")
        self.assertEqual(len(df), 11)
        self.assertEqual(sorted(set(self.sleeps))[0], 0.25)                                   # default 4 requests per second
        self.assertTrue(any(s >= 2 for s in self.sleeps))                                     # backed off after the 429s
        dead = FakeBitstamp(t0, t0, 3600, fail_first=99)
        with self.assertRaises(RuntimeError):
            self.provider(dead, retries=3).history(interval="60m")

    def test_inputs_are_validated(self):
        with self.assertRaises(ValueError):
            BitstampProvider("btc/usd", start="2024-01-01")
        with self.assertRaises(ValueError):
            BitstampProvider("btcusd", start="2024-01-01", rate_per_s=100)
        with self.assertRaises(ValueError):
            self.provider(FakeBitstamp(0, 0, 3600)).history(interval="7m")
        empty = lambda url: json.dumps({"data": {"ohlc": []}}).encode()  # noqa: E731
        with self.assertRaisesRegex(ValueError, "no candles"):
            self.provider(empty).history(interval="60m")
        self.assertEqual(STEPS["60m"], 3600)

    def test_archive_records_the_real_source_and_appends_only_new_candles(self):
        t0 = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            a = MarketArchive(tmp)
            info = a.ingest("BTCUSD-BITSTAMP", "60m", provider=self.provider(FakeBitstamp(t0, t0 + 3600 * 99, 3600)))
            self.assertEqual((info["row_count"], info["provider"]), (100, "Bitstamp public API (/api/v2/ohlc)"))
            self.assertIn("BTC/USD", info["adjustment"])
            info2 = a.ingest("BTCUSD-BITSTAMP", "60m", provider=self.provider(FakeBitstamp(t0, t0 + 3600 * 119, 3600)))
            self.assertEqual(info2["row_count"], 120)                                          # merged, not replaced
            self.assertEqual(len(a.read("BTCUSD-BITSTAMP", "60m")), 120)


if __name__ == "__main__":
    unittest.main()
