"""Multiple-testing statistics for parameter searches (no external dependencies beyond numpy).

* deflated_sharpe  - Bailey & Lopez de Prado: probability that the observed Sharpe beats what the best of N zero-edge trials would reach,
                     corrected for sample length, skewness and kurtosis (N = 1 gives the Probabilistic Sharpe Ratio against 0).
* pbo_cscv         - Probability of Backtest Overfitting by combinatorially symmetric cross-validation: how often the in-sample best
                     parameter set ends up below the median out of sample.
All Sharpe ratios here are PER PERIOD (not annualized) unless a name says otherwise.
"""
from __future__ import annotations

import itertools
import math
from statistics import NormalDist

import numpy as np

_N = NormalDist()
EULER_GAMMA = 0.5772156649015329


def sharpe(returns) -> float:
    r = np.asarray(returns, dtype=float)
    if len(r) < 2:
        return 0.0
    sd = r.std(ddof=1)
    return float(r.mean() / sd) if sd > 1e-15 else 0.0


def moments(returns) -> tuple[float, float]:
    """(skewness, raw kurtosis) with kurtosis 3 for a normal distribution."""
    r = np.asarray(returns, dtype=float)
    if len(r) < 4:
        return 0.0, 3.0
    d = r - r.mean()
    m2 = float((d ** 2).mean())
    if m2 <= 1e-30:
        return 0.0, 3.0
    return float((d ** 3).mean() / m2 ** 1.5), float((d ** 4).mean() / m2 ** 2)


def expected_max_sharpe(var_sr: float, n_trials: int) -> float:
    """Expected maximum per-period Sharpe among n_trials zero-edge trials whose Sharpes have variance var_sr."""
    if n_trials <= 1 or var_sr <= 0:
        return 0.0
    return math.sqrt(var_sr) * ((1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / n_trials) + EULER_GAMMA * _N.inv_cdf(1 - 1 / (n_trials * math.e)))


def deflated_sharpe(sr: float, n_obs: int, skew: float, kurt: float, n_trials: int, var_sr: float):
    """Probability (0..1) that the true Sharpe exceeds the selection-bias hurdle. None if it cannot be computed."""
    if n_obs < 4:
        return None
    sr0 = expected_max_sharpe(var_sr, n_trials)
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 1e-12:
        return None
    return float(_N.cdf((sr - sr0) * math.sqrt(n_obs - 1) / math.sqrt(denom)))


def _col_sharpe(m: np.ndarray) -> np.ndarray:
    sd = m.std(axis=1, ddof=1)
    return np.where(sd > 1e-15, m.mean(axis=1) / np.where(sd > 1e-15, sd, 1.0), 0.0)


def pbo_cscv(matrix, blocks: int = 8):
    """matrix: cells x time returns (aligned). Returns dict(pbo, combos, mean_logit) or None if it cannot be estimated."""
    m = np.asarray(matrix, dtype=float)
    n, t = m.shape
    if n < 2 or blocks < 4 or blocks % 2 or t < blocks * 5:
        return None
    size = t // blocks
    parts = [m[:, i * size:(i + 1) * size] for i in range(blocks)]
    logits = []
    for ins in itertools.combinations(range(blocks), blocks // 2):
        oos = [b for b in range(blocks) if b not in ins]
        sr_is = _col_sharpe(np.concatenate([parts[b] for b in ins], axis=1))
        sr_oos = _col_sharpe(np.concatenate([parts[b] for b in oos], axis=1))
        best = int(np.argmax(sr_is))
        rank = 1 + int((sr_oos < sr_oos[best]).sum()) + 0.5 * int((sr_oos == sr_oos[best]).sum() - 1)   # 1 = worst, ties share
        w = rank / (n + 1)
        logits.append(math.log(w / (1 - w)))
    logits = np.array(logits)
    return {"pbo": float((logits <= 0).mean()), "combos": int(len(logits)), "mean_logit": float(logits.mean())}


def bootstrap_mean_ci(values, n: int = 4000, seed: int = 0):
    v = np.asarray(values, dtype=float)
    if len(v) < 8:
        return None
    rng = np.random.default_rng(seed)
    means = v[rng.integers(0, len(v), size=(n, len(v)))].mean(axis=1)
    return [float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))]
