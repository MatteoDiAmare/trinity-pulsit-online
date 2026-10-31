"""Studies: a pre-registered parameter grid evaluated as ONE piece of research - walk-forward, over several markets,
with multiple-testing correction.

What a study does
* Cartesian grid over parameters (dotted paths, e.g. "short" or "params.fast"), capped at max_cells; never silently truncated.
* Rolling-origin folds per market (walkforward.py). For every fold the parameter set is chosen ONLY from the fold's training window
  (mean annualized Sharpe over the markets, with a minimum number of training trades); that choice is then run on the unseen test window.
  The concatenated test results of those choices (the "WFO series") are the honest out-of-sample evidence of the whole search procedure.
* Markets are pooled with equal weight (one account per market, risk-based sizing and costs from the instrument spec).
* Deflated Sharpe of the WFO series (N = cells searched + trials already spent on these markets), probability of backtest overfitting
  (CSCV over the cell x time return matrix), grid robustness, fold and market consistency, trade-level expectancy with a bootstrap interval,
  and buy-and-hold on the same dates.
* Only bars before each market's development end are ever handed to the simulation: the protected final-test region is untouched.
Verdict `candidate` needs ALL of: WFO Sharpe > 0, DSR >= 0.95, expectancy CI lower bound > 0, PBO <= 0.5, positive in >= 60 % of folds and in
at least two thirds of the markets. Otherwise `no_evidence`, with the failed criteria listed. A candidate is a candidate for FREEZING and
forward tracking, not a finished strategy.
"""
from __future__ import annotations

import concurrent.futures as cf
import copy
import hashlib
import itertools
import json
import math
import os
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .evaluation import compute_metrics
from .instruments import load_instrument
from .simulator import SimConfig, Simulator
from .stats import bootstrap_mean_ci, deflated_sharpe, expected_max_sharpe, moments, pbo_cscv, sharpe
from .strategies import build_strategy
from .walkforward import make_folds

MAX_CELLS = 60
THRESHOLDS = {"dsr_min": 0.95, "pbo_max": 0.5, "positive_folds_min": 0.6, "positive_markets_min": 2 / 3}


def set_path(params: dict, path: str, value):
    out = copy.deepcopy(params)
    keys = path.split(".")
    cur = out
    for k in keys[:-1]:
        if not isinstance(cur, dict) or k not in cur:
            raise ValueError(f"grid path {path!r}: {k!r} does not exist in base_params")
        cur = cur[k]
    if not isinstance(cur, dict):
        raise ValueError(f"grid path {path!r} does not point into a dictionary")
    cur[keys[-1]] = value
    return out


@dataclass(frozen=True)
class StudySpec:
    name: str
    strategy: str
    base_params: dict
    grid: dict
    instruments: list
    interval: str = "1d"
    train_bars: int = 750
    test_bars: int = 125
    step: int | None = None
    embargo: int = 0
    anchored: bool = False
    min_train_trades: int = 10
    prior_trials: int = 0
    capital: float = 1000.0
    max_cells: int = MAX_CELLS

    def __post_init__(self):
        if not self.name.strip() or not self.instruments or not self.grid:
            raise ValueError("a study needs a name, instruments and a non-empty grid")
        if self.max_cells > MAX_CELLS:
            raise ValueError(f"max_cells may not exceed {MAX_CELLS}")
        for path, values in self.grid.items():
            if not isinstance(values, list) or not values or len(set(json.dumps(v, sort_keys=True) for v in values)) != len(values):
                raise ValueError(f"grid[{path!r}] must be a non-empty list of distinct values")
        n = 1
        for v in self.grid.values():
            n *= len(v)
        if n > self.max_cells:
            raise ValueError(f"the grid has {n} cells, more than max_cells={self.max_cells}; shrink it (a study is never truncated silently)")
        if self.prior_trials < 0 or self.min_train_trades < 0:
            raise ValueError("prior_trials and min_train_trades must be >= 0")

    def cells(self) -> list[dict]:
        paths = sorted(self.grid)
        out = []
        for combo in itertools.product(*[self.grid[p] for p in paths]):
            params = self.base_params
            for p, v in zip(paths, combo):
                params = set_path(params, p, v)
            out.append(params)
        return out

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "StudySpec":
        return StudySpec(**d)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------- simulation workers
_G: dict = {}


def _init(payload):
    _G.clear()
    _G.update(payload)


def _segment(res, capital, interval):
    m = compute_metrics(res, interval)
    eq = res.equity["equity"].to_numpy(dtype=float)
    prev = np.concatenate([[capital], eq[:-1]])
    rets = np.where(prev > 0, eq / np.where(prev > 0, prev, 1) - 1, 0.0)
    return {"sharpe": m["sharpe_annualized"] or 0.0, "trades": m["trades"], "net_return_pct": m["net_return_pct"] or 0.0,
            "dates": [str(ts.date()) for ts in res.equity.index], "rets": [float(x) for x in rets],
            "pnls": [float(t["net_pnl"]) for t in res.trades]}


def _eval_task(task):
    cell, params, iid = task
    bars = _G["bars"][iid]
    cfg = SimConfig.from_dict(_G["cfgs"][iid])
    out = {"cell": cell, "iid": iid, "folds": []}
    try:
        for f in _G["folds"][iid]:
            tr = Simulator(cfg).run(bars, build_strategy(_G["strategy"], params), trade_start=f["train_start"], trade_end=f["train_end"])
            te = Simulator(cfg).run(bars, build_strategy(_G["strategy"], params), trade_start=f["test_start"], trade_end=f["test_end"])
            out["folds"].append({"fold": f["index"], "train": _segment(tr, cfg.capital, _G["interval"]), "test": _segment(te, cfg.capital, _G["interval"])})
    except Exception as exc:       # a broken cell (e.g. sandbox violation) is excluded but still counts as a trial
        out["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    return out


# ---------------------------------------------------------------- run
def _f(x, nd=6):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), nd)


def run_study(spec: StudySpec, bars_by_instrument: dict, dev_end: dict, *, workers: int = 1) -> dict:
    """bars_by_instrument: id -> DataFrame (full history); dev_end: id -> first index NOT usable. Nothing at/after dev_end is passed on."""
    ids = list(spec.instruments)
    specs = {i: load_instrument(i) for i in ids}
    dev = {i: bars_by_instrument[i].iloc[:dev_end[i]] for i in ids}
    folds = {i: make_folds(len(dev[i]), spec.train_bars, spec.test_bars, step=spec.step, embargo=spec.embargo, anchored=spec.anchored) for i in ids}
    cells = spec.cells()
    payload = {"bars": dev, "strategy": spec.strategy, "interval": spec.interval,
               "cfgs": {i: SimConfig(spec.capital, None, None, instrument=specs[i]).to_dict() for i in ids},
               "folds": {i: [vars(f) for f in folds[i]] for i in ids}}
    tasks = [(c, p, i) for c, p in enumerate(cells) for i in ids]
    if workers > 1:
        with cf.ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(payload,)) as ex:
            results = list(ex.map(_eval_task, tasks, chunksize=max(1, len(tasks) // (workers * 4))))
    else:
        _init(payload)
        results = [_eval_task(t) for t in tasks]
    return _aggregate(spec, cells, ids, specs, dev, folds, results)


def _aggregate(spec, cells, ids, specs, dev, folds, results):
    n_fold = {i: len(folds[i]) for i in ids}
    res = {(r["cell"], r["iid"]): r for r in results}
    valid = [c for c in range(len(cells)) if all("error" not in res[(c, i)] for i in ids)]
    errors = {c: next(res[(c, i)]["error"] for i in ids if "error" in res[(c, i)]) for c in range(len(cells)) if c not in valid}
    bpy = float(np.mean([specs[i].bars_per_year(spec.interval) for i in ids]))
    n_inst = len(ids)
    fold_ids = sorted({f.index for i in ids for f in folds[i]})

    def seg(c, i, fi, which):
        for s in res[(c, i)]["folds"]:
            if s["fold"] == fi:
                return s[which]
        return None

    def series(parts):
        """parts: list of test segments -> {date: portfolio return} (equal weight, absent market = 0)"""
        acc = {}
        for s in parts:
            for d, r in zip(s["dates"], s["rets"]):
                acc[d] = acc.get(d, 0.0) + r / n_inst
        return acc

    # cell-level out-of-sample series over all folds
    cell_series = {c: series([seg(c, i, fi, "test") for fi in fold_ids for i in ids if seg(c, i, fi, "test")]) for c in valid}
    all_dates = sorted({d for s in cell_series.values() for d in s})
    mat = np.array([[cell_series[c].get(d, 0.0) for d in all_dates] for c in valid]) if valid and all_dates else np.zeros((0, 0))
    cell_sr = np.array([sharpe(row) for row in mat]) if len(mat) else np.array([])
    var_sr = float(np.var(cell_sr, ddof=1)) if len(cell_sr) >= 2 else 0.0
    n_trials = len(cells) + spec.prior_trials

    # walk-forward selection on TRAIN only
    fold_rows, wfo_parts, wfo_pnls, wfo_inst_ret = [], [], [], {i: 0.0 for i in ids}
    for fi in fold_ids:
        best, best_score = None, -math.inf
        for c in valid:
            trs = [seg(c, i, fi, "train") for i in ids if seg(c, i, fi, "train")]
            if not trs or sum(t["trades"] for t in trs) < spec.min_train_trades:
                continue
            score = float(np.mean([t["sharpe"] for t in trs]))
            if score > best_score:
                best, best_score = c, score
        row = {"fold": fi, "selected_cell": best, "train_score": _f(best_score) if best is not None else None}
        if best is not None:
            tests = [seg(best, i, fi, "test") for i in ids if seg(best, i, fi, "test")]
            wfo_parts.extend(tests)
            for i in ids:
                s = seg(best, i, fi, "test")
                if s:
                    wfo_pnls.extend(s["pnls"])
                    wfo_inst_ret[i] += float(np.prod([1 + r for r in s["rets"]]) - 1)
            row["oos_return_pct"] = _f(sum(series(tests).values()) * 100, 3)
            row["oos_trades"] = sum(t["trades"] for t in tests)
            row["params"] = cells[best]
            row["test_dates"] = [min(d for t in tests for d in t["dates"]), max(d for t in tests for d in t["dates"])]
        else:
            row["note"] = "no eligible parameter set in the training window; this fold stays flat"
        fold_rows.append(row)
    wfo_series = series(wfo_parts)
    w_dates = sorted({d for i in ids for fi in fold_ids if valid and seg(valid[0], i, fi, 'test') for d in seg(valid[0], i, fi, 'test')['dates']})   # every test day, flat folds included
    w = np.array([wfo_series.get(d, 0.0) for d in w_dates])
    sr_w = sharpe(w) if len(w) else 0.0
    skew, kurt = moments(w) if len(w) else (0.0, 3.0)
    dsr_w = deflated_sharpe(sr_w, len(w), skew, kurt, n_trials, var_sr) if len(w) else None

    # best single cell (selected on the OOS itself -> optimistic; shown with its deflation)
    best_c = int(np.argmax(cell_sr)) if len(cell_sr) else None
    best_info = None
    if best_c is not None:
        bsr = float(cell_sr[best_c]); sk, ku = moments(mat[best_c])
        best_info = {"cell": valid[best_c], "params": cells[valid[best_c]], "oos_sharpe_annualized": _f(bsr * math.sqrt(bpy), 3),
                     "dsr": _f(deflated_sharpe(bsr, mat.shape[1], sk, ku, n_trials, var_sr), 4)}
    pbo = pbo_cscv(mat) if len(mat) >= 2 else None

    # benchmark: buy-and-hold on the WFO dates (equal weight)
    bh = np.zeros(len(w_dates))
    for i in ids:
        cl = dev[i]["Close"].pct_change().fillna(0.0)
        cl.index = [str(t.date()) for t in cl.index]
        bh += np.array([float(cl.get(d, 0.0)) for d in w_dates]) / n_inst
    exp_ci = bootstrap_mean_ci(wfo_pnls)
    folds_with_trades = [r for r in fold_rows if r.get("oos_return_pct") is not None]
    pos_folds = float(np.mean([r["oos_return_pct"] > 0 for r in folds_with_trades])) if folds_with_trades else 0.0
    pos_markets = int(sum(v > 0 for v in wfo_inst_ret.values()))
    wins = sum(p > 0 for p in wfo_pnls)
    wfo = {"oos_days": len(w), "sharpe_annualized": _f(sr_w * math.sqrt(bpy), 3), "return_pct": _f(float(np.prod(1 + w) - 1) * 100, 3) if len(w) else None,
           "trades": len(wfo_pnls), "hit_rate": _f(wins / len(wfo_pnls), 4) if wfo_pnls else None,
           "expectancy_per_trade": _f(float(np.mean(wfo_pnls)), 4) if wfo_pnls else None, "expectancy_ci95": exp_ci,
           "avg_win": _f(float(np.mean([p for p in wfo_pnls if p > 0])), 3) if wins else None,
           "avg_loss": _f(float(np.mean([p for p in wfo_pnls if p < 0])), 3) if any(p < 0 for p in wfo_pnls) else None,
           "dsr": _f(dsr_w, 4), "positive_fold_share": _f(pos_folds, 3), "positive_markets": f"{pos_markets}/{n_inst}",
           "market_returns_pct": {i: _f(v * 100, 3) for i, v in wfo_inst_ret.items()}}
    bench = {"buy_and_hold_sharpe_annualized": _f(sharpe(bh) * math.sqrt(bpy), 3) if len(bh) else None,
             "buy_and_hold_return_pct": _f(float(np.prod(1 + bh) - 1) * 100, 3) if len(bh) else None,
             "grid_median_oos_sharpe_annualized": _f(float(np.median(cell_sr)) * math.sqrt(bpy), 3) if len(cell_sr) else None,
             "grid_share_positive_oos": _f(float((cell_sr > 0).mean()), 3) if len(cell_sr) else None,
             "expected_best_of_n_noise_sharpe_annualized": _f(expected_max_sharpe(var_sr, n_trials) * math.sqrt(bpy), 3)}
    # verdict
    reasons = []
    if not (sr_w > 0):
        reasons.append("walk-forward out-of-sample Sharpe is not positive")
    if dsr_w is None or dsr_w < THRESHOLDS["dsr_min"]:
        reasons.append(f"deflated Sharpe {('%.2f' % dsr_w) if dsr_w is not None else 'n/a'} < {THRESHOLDS['dsr_min']} (N={n_trials} trials)")
    if not exp_ci or exp_ci[0] <= 0:
        reasons.append("expectancy per trade is not significantly above zero (95% bootstrap interval)")
    if pbo is None or pbo["pbo"] > THRESHOLDS["pbo_max"]:
        reasons.append(f"probability of backtest overfitting {('%.2f' % pbo['pbo']) if pbo else 'n/a'} > {THRESHOLDS['pbo_max']}")
    if pos_folds < THRESHOLDS["positive_folds_min"]:
        reasons.append(f"only {pos_folds:.0%} of folds are positive")
    if n_inst > 1 and pos_markets < math.ceil(n_inst * THRESHOLDS["positive_markets_min"]):
        reasons.append(f"positive in only {pos_markets}/{n_inst} markets")
    cell_rows = []
    for c in range(len(cells)):
        row = {"cell": c, "params": cells[c], "valid": c in valid}
        if c in errors:
            row["error"] = errors[c]
        elif c in valid:
            k = valid.index(c)
            pn = [p for fi in fold_ids for i in ids if seg(c, i, fi, "test") for p in seg(c, i, fi, "test")["pnls"]]
            row.update({"oos_sharpe_annualized": _f(cell_sr[k] * math.sqrt(bpy), 3), "oos_trades": len(pn),
                        "oos_expectancy": _f(float(np.mean(pn)), 4) if pn else None, "oos_hit_rate": _f(sum(p > 0 for p in pn) / len(pn), 3) if pn else None,
                        "selected_in_folds": sum(1 for r in fold_rows if r.get("selected_cell") == c)})
        cell_rows.append(row)
    return {"study": spec.name, "spec_sha256": spec.sha256, "cells": len(cells), "valid_cells": len(valid), "n_trials_for_deflation": n_trials,
            "markets": ids, "folds": {i: n_fold[i] for i in ids}, "data_used": {i: f"bars 0..{len(dev[i]) - 1} ({dev[i].index[0].date()}..{dev[i].index[-1].date()})" for i in ids},
            "wfo": wfo, "best_single_cell": best_info, "pbo": pbo, "benchmarks": bench, "verdict": "candidate" if not reasons else "no_evidence",
            "failed_criteria": reasons, "thresholds": THRESHOLDS, "fold_table": fold_rows, "cell_table": cell_rows,
            "note": "Candidate = worth freezing and tracking forward, not proven. No_evidence = the search procedure did not beat what noise would produce."}


# ---------------------------------------------------------------- registry integration
def run_recorded_study(registry, archive, spec: StudySpec, *, workers: int = 1) -> dict:
    """Loads archived bars, respects every market's consumed-test frontier / protected tail, deduplicates, runs and records the study.
    Trials already spent on these markets (the largest per-market count) raise the deflation hurdle, and every cell of the study is added to the trial counts."""
    from .integrity import SpecIndex, code_version, dataset_sha256, market_key, test_frontier
    from .walkforward import development_end
    bars, dev_end = {}, {}
    for i in spec.instruments:
        s = load_instrument(i)
        bars[i] = archive.read(s.symbol, spec.interval)
        dev_end[i] = development_end(bars[i], test_frontier(registry, s.symbol, spec.interval))
    dev_sha = {i: dataset_sha256(bars[i].iloc[:dev_end[i]]) for i in spec.instruments}
    study_sha = hashlib.sha256(json.dumps({"spec": spec.to_dict(), "dev": dev_sha, "code": code_version()}, sort_keys=True, default=str).encode()).hexdigest()
    for rid, cfg_json, res_json in registry.db.execute("SELECT id, configuration_json, results_json FROM experiments WHERE name='academy:study'"):
        if json.loads(cfg_json).get("study_sha256") == study_sha:
            return {**json.loads(res_json), "reused": True, "registry_id": rid}
    index = SpecIndex(registry)
    markets = {i: market_key(load_instrument(i).symbol, spec.interval) for i in spec.instruments}
    # one pooled series per cell: trials already spent on the markets raise the hurdle by the largest per-market count (not their sum)
    prior = max([spec.prior_trials] + [index.trials(m) for m in set(markets.values())])
    eff = StudySpec(**{**spec.to_dict(), "prior_trials": prior})
    result = run_study(eff, bars, dev_end, workers=workers)
    result["study_sha256"] = study_sha
    result["prior_trials_counted"] = prior
    cfg = {"spec": spec.to_dict(), "study_sha256": study_sha, "dev_end": dev_end, "dev_dataset_sha256": dev_sha, "code_version": code_version(),
           "prior_trials": prior}
    rid = registry.record("academy:study", pd.concat({i: bars[i].iloc[:dev_end[i]] for i in spec.instruments}), cfg, result)
    for c in range(result["cells"]):
        for i in spec.instruments:
            index.add(hashlib.sha256(f"{study_sha}|{c}|{i}".encode()).hexdigest(), rid, markets[i])
    result["registry_id"], result["reused"] = rid, False
    return result
