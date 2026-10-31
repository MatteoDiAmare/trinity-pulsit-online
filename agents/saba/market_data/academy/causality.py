"""Causality gate: every non-built-in strategy must prove it does not use future information.

Black-box checks (work for specs and for any future custom strategy). What they DO catch: unseeded randomness,
hidden global state, order-of-calls effects and dependence on data beyond the cut. What they CANNOT catch: a
strategy that reads data through a side channel (files, globals, the archive). Custom-code strategies must
therefore run in an isolated runner with no data access (next milestone); JSON specs have no such channel:
  1. determinism     - two identical runs give identical decisions;
  2. truncation      - decisions on bars[:k] equal the decisions of the full run on the same bars;
  3. future tamper   - rewriting all bars after k (scaling prices) does not change any decision up to k.
A failure rejects the strategy before any experiment is recorded.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .simulator import SimConfig, Simulator
from .strategies import build_strategy


def _decisions(bars, name, params, cfg):
    res = Simulator(cfg).run(bars, build_strategy(name, params))
    return [(d["timestamp"], d["action"], d["stop_loss_pct"], d["take_profit_pct"], d["size"]) for d in res.decisions]


def check_causal(name: str, params: dict, bars: pd.DataFrame, cfg: SimConfig | None = None, *,
                 max_bars: int = 600, cuts: int = 3, seed: int = 0) -> dict:
    cfg = cfg or SimConfig()
    bars = bars.iloc[-max_bars:] if len(bars) > max_bars else bars
    if len(bars) < 60:
        return {"ok": False, "reason": "too few bars to verify causality", "checks": {}}
    rng = np.random.default_rng(seed)
    full = _decisions(bars, name, params, cfg)
    checks = {"deterministic": full == _decisions(bars, name, params, cfg)}
    lo, hi = len(bars) // 3, len(bars) - 5
    cut_points = sorted(int(x) for x in rng.integers(lo, hi, size=cuts))
    trunc_ok = tamper_ok = True
    for k in cut_points:
        cutoff = bars.index[k - 1]
        expected = [d for d in full if d[0] < cutoff]
        short = _decisions(bars.iloc[:k], name, params, cfg)
        trunc_ok &= expected == [d for d in short if d[0] < cutoff]
        tampered = bars.copy()
        cols = ["Open", "High", "Low", "Close"]
        tampered.iloc[k:, [tampered.columns.get_loc(c) for c in cols]] *= float(rng.uniform(0.3, 3.0))
        tampered.iloc[k:, tampered.columns.get_loc("Volume")] = 0.0
        t_dec = _decisions(tampered, name, params, cfg)
        tamper_ok &= expected == [d for d in t_dec if d[0] < cutoff]
    checks.update({"truncation_invariant": bool(trunc_ok), "future_tamper_invariant": bool(tamper_ok)})
    ok = all(checks.values())
    return {"ok": ok, "checks": checks, "cut_points": cut_points,
            "reason": None if ok else "failed: " + ", ".join(k for k, v in checks.items() if not v)}


def assert_causal(name, params, bars, cfg=None, **kw) -> dict:
    report = check_causal(name, params, bars, cfg, **kw)
    if not report["ok"]:
        raise ValueError(f"causality gate failed for strategy {name}: {report['reason']}")
    return report
