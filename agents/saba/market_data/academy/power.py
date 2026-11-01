"""Statistical power of a pooled multi-market walk-forward study (analytic, no simulation).

Model: a strategy with the same true Sharpe `s` on each market; markets have average pairwise correlation rho of their strategy returns. The
equal-weight pooled Sharpe is s * sqrt(N_eff) with N_eff = N / (1 + (N-1) rho). Over `years` of out-of-sample data the Sharpe estimator has
standard error ~ sqrt((1 + SR^2/2) / years). To pass a deflated-Sharpe test the pooled estimate must exceed the best-of-N noise level SR0 by
z_alpha standard errors; with power `1-beta` the true pooled Sharpe must exceed SR0 + (z_alpha + z_beta) standard errors.
These are approximations (normal returns, constant correlation); they say how the minimum detectable edge shrinks with more markets and
more years, not what any particular strategy will do.
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd

from .stats import expected_max_sharpe

_N = NormalDist()


def effective_markets(returns: pd.DataFrame) -> dict:
    """returns: DataFrame (dates x markets) of daily returns (buy-and-hold or strategy). Uses the mean pairwise correlation on shared dates."""
    corr = returns.corr(min_periods=250)
    n = corr.shape[0]
    off = corr.to_numpy()[~np.eye(n, dtype=bool)]
    rho = float(np.nanmean(off)) if len(off) else 0.0
    rho = max(rho, 0.0)
    return {"markets": n, "mean_pairwise_correlation": rho, "effective_independent_markets": float(n / (1 + (n - 1) * rho)) if n else 0.0}


def sharpe_se(sr_annual: float, years: float) -> float:
    return math.sqrt((1 + 0.5 * sr_annual ** 2) / max(years, 1e-9))


def min_detectable_sharpe(years: float, n_effective: float, n_trials: int, var_sr_period: float = 0.0, *, alpha: float = 0.05, power: float = 0.8,
                          periods_per_year: float = 252.0) -> dict:
    """Smallest TRUE per-market annualized Sharpe detected with the given power by a pooled test over `years`."""
    sr0_annual = expected_max_sharpe(var_sr_period, n_trials) * math.sqrt(periods_per_year)
    z_a, z_b = _N.inv_cdf(1 - alpha), _N.inv_cdf(power)
    pooled = sr0_annual
    for _ in range(500):                    # SE depends on the Sharpe itself: fixed-point iteration (a contraction for realistic inputs)
        pooled = sr0_annual + (z_a + z_b) * sharpe_se(pooled, years)
    return {"years": years, "n_effective_markets": n_effective, "n_trials": n_trials, "hurdle_best_of_n_annual": sr0_annual,
            "pooled_sharpe_needed": pooled, "per_market_sharpe_needed": pooled / math.sqrt(max(n_effective, 1e-9))}
