"""Deterministic, explainable OHLC candle anatomy and descriptive patterns.

Patterns are labels for observed geometry, never buy/sell recommendations.
Only completed, timestamped bars should be supplied by the caller.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def anatomy(bars: pd.DataFrame) -> pd.DataFrame:
    required = ("Open", "High", "Low", "Close")
    if any(k not in bars for k in required):
        raise ValueError("OHLC columns required")
    if not bars.index.is_monotonic_increasing or bars.index.has_duplicates:
        raise ValueError("index must be sorted and unique")
    p = bars.loc[:, required].astype(float)
    if not np.isfinite(p.to_numpy()).all():
        raise ValueError("OHLC must be finite")
    o, h, l, c = (p[k] for k in required)
    if ((h < p[["Open", "Close", "Low"]].max(axis=1)) |
        (l > p[["Open", "Close", "High"]].min(axis=1))).any():
        raise ValueError("inconsistent OHLC")
    span = h - l
    body = (c - o).abs()
    upper = h - pd.concat([o, c], axis=1).max(axis=1)
    lower = pd.concat([o, c], axis=1).min(axis=1) - l
    nonzero = span.where(span > 0, np.nan)
    return pd.DataFrame({
        "range": span, "body": body, "upper_wick": upper, "lower_wick": lower,
        "signed_body": c - o, "body_fraction": body / nonzero,
        "upper_fraction": upper / nonzero, "lower_fraction": lower / nonzero,
        "close_location": (2 * c - h - l) / nonzero,
    }, index=bars.index)


def describe_patterns(bars: pd.DataFrame, *, doji_fraction: float = 0.10,
                      wick_body_ratio: float = 2.0) -> pd.DataFrame:
    """Descriptive geometry only; names are intentionally *-shaped.

    Context-dependent names such as hammer/shooting star are NOT assigned.
    No lookahead or future bars are used.
    """
    if not 0 <= doji_fraction <= 1 or wick_body_ratio <= 0:
        raise ValueError("invalid thresholds")
    a = anatomy(bars)
    valid = a["range"] > 0
    body = a["body"]
    # A wick/body ratio is undefined for a zero-body doji; use a tiny positive
    # denominator only to classify geometry, not to claim a predictive pattern.
    effective_body = body.where(body > 0, a["range"] * 1e-12)
    result = pd.DataFrame(index=bars.index)
    result["bullish"] = bars["Close"] > bars["Open"]
    result["bearish"] = bars["Close"] < bars["Open"]
    result["doji_like"] = valid & (a["body_fraction"] <= doji_fraction)
    result["long_lower_wick"] = valid & (a["lower_wick"] >= wick_body_ratio * effective_body) & (a["lower_wick"] > a["upper_wick"])
    result["long_upper_wick"] = valid & (a["upper_wick"] >= wick_body_ratio * effective_body) & (a["upper_wick"] > a["lower_wick"])
    result["inside_bar"] = (bars["High"] < bars["High"].shift(1)) & (bars["Low"] > bars["Low"].shift(1))
    result["outside_bar"] = (bars["High"] > bars["High"].shift(1)) & (bars["Low"] < bars["Low"].shift(1))
    result["bullish_body_engulfing"] = (
        result["bullish"] & (bars["Close"].shift(1) < bars["Open"].shift(1)) &
        (bars["Open"] <= bars["Close"].shift(1)) & (bars["Close"] >= bars["Open"].shift(1)))
    result["bearish_body_engulfing"] = (
        result["bearish"] & (bars["Close"].shift(1) > bars["Open"].shift(1)) &
        (bars["Open"] >= bars["Close"].shift(1)) & (bars["Close"] <= bars["Open"].shift(1)))
    return result.fillna(False).astype(bool)
