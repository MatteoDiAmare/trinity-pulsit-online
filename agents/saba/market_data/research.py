"""Small deterministic baseline report; not a predictive signal or trading advice."""
from __future__ import annotations

import pandas as pd

from .archive import MarketArchive


def describe(symbol: str, interval: str = "1d", root=None) -> dict:
    prices = MarketArchive(root).read(symbol, interval)["Close"].astype(float)
    if len(prices) < 2:
        raise ValueError("at least two observations required")
    changes = prices.pct_change().dropna()
    running_peak = prices.cummax()
    drawdown = (prices / running_peak - 1).min()
    return {
        "symbol": symbol,
        "interval": interval,
        "observations": len(prices),
        "start_utc": prices.index[0].isoformat(),
        "end_utc": prices.index[-1].isoformat(),
        "first_close": float(prices.iloc[0]),
        "last_close": float(prices.iloc[-1]),
        "close_change_pct": float((prices.iloc[-1] / prices.iloc[0] - 1) * 100),
        "max_close_drawdown_pct": float(drawdown * 100),
        "bar_return_std_pct": float(changes.std(ddof=0) * 100),
        "note": "Descriptive historical statistics; no backtest, transaction costs or forecasting.",
    }
