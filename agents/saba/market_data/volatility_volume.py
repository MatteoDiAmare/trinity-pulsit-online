"""Causal, descriptive volatility and volume features for completed OHLC bars.

No directional trade signal is produced. Current bar is compared against
historical baselines that exclude it. Missing volume remains missing.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def volatility_volume_features(
    bars: pd.DataFrame, *, window: int = 20, min_periods: int | None = None
) -> pd.DataFrame:
    if window < 2:
        raise ValueError("window must be >= 2")
    if min_periods is None:
        min_periods = window
    if not 2 <= min_periods <= window:
        raise ValueError("min_periods must be between 2 and window")
    if not bars.index.is_monotonic_increasing or bars.index.has_duplicates:
        raise ValueError("index must be sorted and unique")
    if not {"Open", "High", "Low", "Close"}.issubset(bars.columns):
        raise ValueError("OHLC columns required")
    p = bars[["Open", "High", "Low", "Close"]].astype(float)
    if not np.isfinite(p.to_numpy()).all() or (p <= 0).any().any():
        raise ValueError("OHLC must be finite and positive")
    if ((p["High"] < p[["Open", "Close", "Low"]].max(axis=1)) |
        (p["Low"] > p[["Open", "Close", "High"]].min(axis=1))).any():
        raise ValueError("inconsistent OHLC")
    close = p["Close"]
    previous_close = close.shift(1)
    tr = pd.concat([
        p["High"] - p["Low"],
        (p["High"] - previous_close).abs(),
        (p["Low"] - previous_close).abs(),
    ], axis=1).max(axis=1)
    log_return = np.log(close / previous_close)
    # Historical baseline excludes the current bar; features at bar t are
    # only usable once t has closed.
    prior_atr = tr.shift(1).rolling(window, min_periods=min_periods).mean()
    prior_volatility = log_return.shift(1).rolling(
        window, min_periods=min_periods).std()
    out = pd.DataFrame({
        "true_range": tr,
        "prior_atr": prior_atr,
        "range_to_prior_atr": (p["High"] - p["Low"]) / prior_atr.where(prior_atr > 0),
        "log_return": log_return,
        "prior_realized_volatility": prior_volatility,
        "absolute_return_to_prior_volatility": log_return.abs() / prior_volatility.where(prior_volatility > 0),
    }, index=bars.index)
    if "Volume" in bars.columns:
        volume = pd.to_numeric(bars["Volume"], errors="coerce").astype(float)
        if (volume.dropna() < 0).any() or np.isinf(volume.to_numpy()).any():
            raise ValueError("volume must be nonnegative or missing")
        prior_volume = volume.shift(1).rolling(
            window, min_periods=min_periods).mean()
        out["volume"] = volume
        out["prior_mean_volume"] = prior_volume
        out["relative_volume"] = volume / prior_volume.where(prior_volume > 0)
    return out
