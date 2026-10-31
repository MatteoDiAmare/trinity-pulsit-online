"""Stress-test data transforms (the execution-side stress lives in risk.StressModel)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def inject_gaps(bars: pd.DataFrame, *, probability: float, size_pct: float, seed: int = 0) -> pd.DataFrame:
    """Return a copy where, with `probability` per bar, price jumps by +/- size_pct between the previous
    close and this open, and the jump persists in all later bars (a genuine level shift, no phantom fill).
    Deterministic for a given seed."""
    if not 0 <= probability <= 1 or size_pct <= 0:
        raise ValueError("invalid gap parameters")
    rng = np.random.default_rng(seed)
    out = bars.copy()
    cols = [c for c in ("Open", "High", "Low", "Close") if c in out.columns]
    factor = 1.0
    factors = np.ones(len(out))
    for i in range(1, len(out)):
        if rng.random() < probability:
            factor *= 1 + (size_pct / 100) * (1 if rng.random() < 0.5 else -1)
        factors[i] = factor
    out[cols] = out[cols].to_numpy() * factors[:, None]
    return out
