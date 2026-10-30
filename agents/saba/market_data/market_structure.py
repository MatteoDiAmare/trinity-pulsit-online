"""Causal, descriptive market structure on completed OHLC bars.

A pivot at t is not visible until t + right completed bars. Returned
confirmed_at timestamps represent the earliest valid observation time.
No trade recommendations or order execution.
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def confirmed_swings(bars: pd.DataFrame, *, left: int = 2, right: int = 2) -> pd.DataFrame:
    if left < 1 or right < 1:
        raise ValueError("left and right must be positive")
    if not bars.index.is_monotonic_increasing or bars.index.has_duplicates:
        raise ValueError("index must be sorted and unique")
    if not {"High", "Low"}.issubset(bars.columns):
        raise ValueError("High and Low required")
    high = bars["High"].to_numpy(dtype=float)
    low = bars["Low"].to_numpy(dtype=float)
    if not (np.isfinite(high).all() and np.isfinite(low).all() and (high >= low).all()):
        raise ValueError("invalid High/Low")
    rows = []
    for i in range(left, len(bars) - right):
        h_neighbors = np.concatenate((high[i-left:i], high[i+1:i+right+1]))
        l_neighbors = np.concatenate((low[i-left:i], low[i+1:i+right+1]))
        if high[i] > h_neighbors.max():
            rows.append({"pivot_at": bars.index[i], "confirmed_at": bars.index[i+right],
                         "kind": "high", "price": float(high[i])})
        if low[i] < l_neighbors.min():
            rows.append({"pivot_at": bars.index[i], "confirmed_at": bars.index[i+right],
                         "kind": "low", "price": float(low[i])})
    return pd.DataFrame(rows, columns=["pivot_at", "confirmed_at", "kind", "price"])


def structure_state(bars: pd.DataFrame, *, left: int = 2, right: int = 2) -> pd.DataFrame:
    """At each completed bar, classify structure using only confirmed pivots.

    Uses latest two confirmed highs and lows; strict comparison means equal
    extrema give 'mixed'. No predictive claim is made.
    """
    swings = confirmed_swings(bars, left=left, right=right)
    states = []
    highs, lows = [], []
    by_confirm = {}
    for rec in swings.to_dict("records"):
        by_confirm.setdefault(rec["confirmed_at"], []).append(rec)
    for stamp in bars.index:
        for rec in by_confirm.get(stamp, []):
            (highs if rec["kind"] == "high" else lows).append(rec["price"])
        if len(highs) < 2 or len(lows) < 2:
            states.append("unknown")
        elif highs[-1] > highs[-2] and lows[-1] > lows[-2]:
            states.append("bullish")
        elif highs[-1] < highs[-2] and lows[-1] < lows[-2]:
            states.append("bearish")
        else:
            states.append("mixed")
    return pd.DataFrame({"structure": states}, index=bars.index)


def prior_range_breaks(bars: pd.DataFrame, *, lookback: int = 20, buffer: float = 0.0) -> pd.DataFrame:
    """Close beyond the *previous* N-bar high/low, descriptive only.

    A rolling range is not the same as a confirmed swing BOS. This simple
    baseline deliberately excludes the current bar to avoid leakage.
    """
    if lookback < 2 or buffer < 0:
        raise ValueError("lookback >= 2 and buffer >= 0 required")
    if not {"High", "Low", "Close"}.issubset(bars.columns):
        raise ValueError("High, Low, Close required")
    prior_high = bars["High"].shift(1).rolling(lookback, min_periods=lookback).max()
    prior_low = bars["Low"].shift(1).rolling(lookback, min_periods=lookback).min()
    return pd.DataFrame({
        "prior_resistance": prior_high,
        "prior_support": prior_low,
        "close_above_prior_range": (bars["Close"] > prior_high + buffer).fillna(False),
        "close_below_prior_range": (bars["Close"] < prior_low - buffer).fillna(False),
    }, index=bars.index)
