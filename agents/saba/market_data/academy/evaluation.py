"""Evaluation: metrics computed from a SimResult. No simulation logic here."""
from __future__ import annotations

import math
import numpy as np
import pandas as pd

# Approximate bars per year for gold futures (~23 h/day, 252 days). Used only to annualize Sharpe.
BARS_PER_YEAR = {"1m": 252 * 23 * 60, "2m": 252 * 23 * 30, "5m": 252 * 23 * 12, "15m": 252 * 23 * 4,
                 "30m": 252 * 23 * 2, "60m": 252 * 23, "1h": 252 * 23, "90m": 252 * 23 * 2 // 3,
                 "1d": 252, "5d": 52, "1wk": 52, "1mo": 12, "3mo": 4}


def _f(x):
    x = float(x)
    return x if math.isfinite(x) else None


def compute_metrics(result, interval: str = "1d") -> dict:
    eq = result.equity["equity"].to_numpy(dtype=float)
    start = result.config.capital
    curve = np.concatenate([[start], eq])
    peak = np.maximum.accumulate(curve)
    drawdown = curve / peak - 1
    max_dd = float(drawdown.min()) * 100
    net_pct = (curve[-1] / start - 1) * 100
    prev = curve[:-1]
    rets = np.where(prev > 0, curve[1:] / np.where(prev > 0, prev, 1) - 1, 0.0)
    sd = rets.std(ddof=1) if len(rets) > 1 else 0.0
    spec = result.config.instrument
    ann = math.sqrt(spec.bars_per_year(interval) if spec else BARS_PER_YEAR.get(interval, 252))
    sharpe = rets.mean() / sd * ann if sd > 0 else 0.0
    trades = result.trades
    n = len(trades)
    wins = [t["net_pnl"] for t in trades if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in trades if t["net_pnl"] < 0]
    return {
        "bars": int(len(eq)),
        "start_equity": float(start),
        "end_equity": _f(curve[-1]),
        "net_return_pct": _f(net_pct),
        "max_drawdown_pct": _f(max_dd),
        "trades": n,
        "hit_rate": _f(len(wins) / n) if n else None,
        "profit_factor": _f(sum(wins) / -sum(losses)) if losses else None,
        "sharpe_annualized": _f(sharpe),
        "return_over_max_drawdown": _f(net_pct / abs(max_dd)) if max_dd < 0 else None,
        "total_fees": _f(sum(t["fees"] for t in trades)),
        "total_spread_slippage": _f(sum(t["spread_slippage_cost"] for t in trades)),
        "ambiguous_trades": int(sum(t["ambiguous"] for t in trades)),
        "rejected_orders": int(sum(1 for o in result.orders if o["kind"] == "rejected")),
        "exposure_fraction": _f((result.equity["position_side"] != 0).mean()),
        "halted": result.halted,
        "shortfall": _f(result.shortfall),
        "risk_events": len(result.risk_events),
        "avg_r_multiple": _f(np.mean([t["r_multiple"] for t in trades if t["r_multiple"] is not None])) if n else None,
        "stop_failures": int(sum(t["exit_reason"] == "stop_failed" for t in trades)),
        "liquidations": int(sum(t["exit_reason"].startswith("liquidation") for t in trades)),
        "gap_stops": int(sum(t["exit_reason"] == "stop_gap" for t in trades)),
    }
