"""Reproducible walk-forward research with source ablation and cost-aware evaluation.

This laboratory intentionally does not place orders or select a live strategy.
Signal generation uses only prior completed bars; execution assumes next bar open.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


def evaluate(prices: pd.DataFrame, features: pd.DataFrame, *, train_fraction: float = 0.65,
             fee_bps: float = 2.0, spread_bps: float = 3.0,
             slippage_bps: float = 2.0) -> dict:
    """Evaluate fixed directional signal on chronological holdout.

    Features must have a 'signal' column with -1/0/1. Signal at bar t is
    assumed available only AFTER bar t closes. Position starts at bar t+1 open,
    held to t+2 open; return uses consecutive opens and signal.shift(1).
    No fitting, optimization, or leverage is performed.
    """
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between 0 and 1")
    if min(fee_bps, spread_bps, slippage_bps) < 0:
        raise ValueError("costs cannot be negative")
    if not prices.index.equals(features.index):
        raise ValueError("price and feature indexes must match exactly")
    if len(prices) < 30:
        raise ValueError("at least 30 bars required")
    signal = features["signal"].astype(float)
    if not signal.isin([-1, 0, 1]).all():
        raise ValueError("signals must be -1, 0, or 1")
    opens = prices["Open"].astype(float)
    if not (opens > 0).all():
        raise ValueError("invalid open price")
    # At row t, signal from t-1 is known; return is open[t] -> open[t+1].
    position = signal.shift(1).fillna(0)
    next_return = opens.shift(-1) / opens - 1
    turnover = (position - position.shift(1).fillna(0)).abs()
    one_way_cost = (fee_bps + spread_bps / 2 + slippage_bps) / 10000
    net = position * next_return - turnover * one_way_cost
    # Account for liquidation of the last open position at the end.
    net.iloc[-2] -= abs(position.iloc[-2]) * one_way_cost
    valid = net.iloc[1:-1]
    cut = max(1, min(len(valid) - 1, int(len(valid) * train_fraction)))
    def metrics(returns, positions):
        equity = (1 + returns).cumprod()
        return {
            "bars": int(len(returns)),
            "net_return_pct": float((equity.iloc[-1] - 1) * 100),
            "max_drawdown_pct": float(((equity / equity.cummax()) - 1).min() * 100),
            "mean_bar_return_pct": float(returns.mean() * 100),
            "positive_bar_fraction": float((returns > 0).mean()),
            "exposure_fraction": float((positions != 0).mean()),
        }
    return {
        "method": "fixed signal, next-open execution, chronological split",
        "cost_bps": {"fee": fee_bps, "spread": spread_bps, "slippage": slippage_bps},
        "train": metrics(valid.iloc[:cut], position.loc[valid.index[:cut]]),
        "holdout": metrics(valid.iloc[cut:], position.loc[valid.index[cut:]]),
        "warning": "Illustrative bar-based model; no financing, margin, overnight gaps beyond observed opens, broker fills, contract rolls or intrabar liquidation.",
    }


def moving_average_signal(prices: pd.DataFrame, short: int = 10, long: int = 30) -> pd.DataFrame:
    """Causal example: moving averages calculated from completed closes."""
    if short < 2 or long <= short:
        raise ValueError("require 2 <= short < long")
    close = prices["Close"].astype(float)
    fast = close.rolling(short, min_periods=long).mean()
    slow = close.rolling(long, min_periods=long).mean()
    signal = pd.Series(np.where(fast > slow, 1, np.where(fast < slow, -1, 0)), index=prices.index)
    return pd.DataFrame({"signal": signal.astype(int)}, index=prices.index)


def source_ablation(prices: pd.DataFrame, candidates: dict[str, pd.DataFrame], **kwargs) -> dict:
    """Compare *predeclared* candidate signals on identical holdout periods.

    These results are exploratory; choosing a winner from the holdout invalidates
    that holdout as a final unbiased test. Reserve a fresh period for confirmation.
    """
    if not candidates:
        raise ValueError("at least one candidate required")
    return {name: evaluate(prices, signals, **kwargs) for name, signals in candidates.items()}


class ExperimentRegistry:
    """Append-only local SQLite journal for reproducible research metadata."""
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS experiments (
            id INTEGER PRIMARY KEY, created_at_utc TEXT NOT NULL,
            name TEXT NOT NULL, dataset_sha256 TEXT NOT NULL,
            configuration_json TEXT NOT NULL, results_json TEXT NOT NULL)""")
        self.db.commit()

    def record(self, name: str, prices: pd.DataFrame, configuration: dict, results: dict) -> int:
        if not name.strip():
            raise ValueError("experiment name required")
        digest = hashlib.sha256(pd.util.hash_pandas_object(prices, index=True).values.tobytes()).hexdigest()
        cursor = self.db.execute(
            "INSERT INTO experiments(created_at_utc,name,dataset_sha256,configuration_json,results_json) VALUES (?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), name, digest,
             json.dumps(configuration, sort_keys=True, allow_nan=False),
             json.dumps(results, sort_keys=True, allow_nan=False)))
        self.db.commit()
        return int(cursor.lastrowid)

    def recent(self, limit: int = 20) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit out of range")
        rows = self.db.execute("SELECT id,created_at_utc,name,dataset_sha256,configuration_json,results_json FROM experiments ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": i, "created_at_utc": t, "name": n, "dataset_sha256": h,
                 "configuration": json.loads(c), "results": json.loads(r)} for i,t,n,h,c,r in rows]

    def close(self):
        self.db.close()
