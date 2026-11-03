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
from collections import Counter
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .evaluation import compute_metrics
from .instruments import load_instrument, usable_from
from .intraday import SessionRules, intraday_costs, session_kind
from .sessions import check_session, interval_minutes, is_intraday
from .simulator import SimConfig, Simulator
from .quality import check_bars, clean_bars
from .risk import StressModel
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
    start_date: str | None = None        # ISO date; None = start of the usable history of each market (see usable_from) - set it to reproduce an older window
    fold_mode: str = "dates"             # "dates": folds on a common calendar (no cross-market look-ahead); "bars": legacy per-market bar index
    pooling: str = "active"              # "active": equal weight over the markets trading that day; "fixed": legacy, divide by the number of markets
    cost_multiplier: float = 1.0         # sensitivity: multiplies spread and slippage of every market
    clean_data: bool = True              # drop provider glitch bars (spike-and-revert, contradicting open); the archive itself is never changed
    respect_usable_from: bool = True     # clip each market to the start of its usable history
    # ---- intraday (all optional; at their defaults they are left out of the recorded spec, so earlier studies keep their hash)
    train_days: int | None = None        # fold_mode "days": window lengths in CALENDAR days (intraday); train_bars / test_bars are then only a minimum-data guard
    test_days: int | None = None
    step_days: int | None = None
    allow_overnight: bool | None = None  # None = daily: carried positions allowed; intraday: NOT allowed (Saba's intraday-only policy). True must be said explicitly
    intraday_costs: bool = True          # intraday: apply the per-instrument time-of-day spread, range-scaled slippage and financing (data/intraday_profiles.json)

    def __post_init__(self):
        if self.fold_mode not in ("dates", "bars", "days") or self.pooling not in ("active", "fixed") or self.cost_multiplier <= 0:
            raise ValueError("fold_mode must be 'dates', 'bars' or 'days', pooling 'active' or 'fixed', cost_multiplier > 0")
        intraday = is_intraday(self.interval)
        if intraday and self.fold_mode != "days":
            raise ValueError(f"interval {self.interval!r} is intraday: fold_mode 'dates' and 'bars' count weekdays / bars of a daily series. Use fold_mode 'days' with "
                             "train_days and test_days (calendar days)")
        if intraday and self.pooling != "active":
            raise ValueError("intraday studies pool with pooling 'active' (equal weight over the markets that trade at each timestamp)")
        if self.fold_mode == "days":
            if not (isinstance(self.train_days, int) and isinstance(self.test_days, int) and self.train_days >= 5 and self.test_days >= 5):
                raise ValueError("fold_mode 'days' needs integer train_days >= 5 and test_days >= 5 (calendar days)")
            if self.step_days is not None and self.step_days < self.test_days:
                raise ValueError("step_days must be >= test_days so that test windows never overlap")
        elif self.train_days is not None or self.test_days is not None or self.step_days is not None:
            raise ValueError("train_days / test_days / step_days belong to fold_mode 'days'")
        if intraday and self.allow_overnight is None:
            object.__setattr__(self, "allow_overnight", False)       # explicit in the recorded spec: the policy is part of the test, never implicit
        if self.start_date is not None:
            pd.Timestamp(self.start_date)
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

    _INTRADAY_DEFAULTS = {"train_days": None, "test_days": None, "step_days": None, "allow_overnight": None, "intraday_costs": True}

    def to_dict(self) -> dict:
        d = asdict(self)
        for k, v in self._INTRADAY_DEFAULTS.items():       # keep the record (and its hash) of every earlier daily study unchanged
            if d.get(k) == v:
                d.pop(k, None)
        return d

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


def _key(ts, intraday: bool) -> str:
    """Pooling key of a bar: the date for daily series (one bar per day), the full timestamp for intraday series."""
    return ts.isoformat() if intraday else str(ts.date())


def _segment(res, capital, interval, keep_series=True):
    m = compute_metrics(res, interval)
    out = {"sharpe": m["sharpe_annualized"] or 0.0, "trades": m["trades"], "net_return_pct": m["net_return_pct"] or 0.0}
    if not keep_series:          # training windows only feed parameter selection: no need to carry their series around
        return out
    eq = res.equity["equity"].to_numpy(dtype=float)
    prev = np.concatenate([[capital], eq[:-1]])
    rets = np.where(prev > 0, eq / np.where(prev > 0, prev, 1) - 1, 0.0)
    intraday = is_intraday(interval)
    out.update({"dates": [_key(ts, intraday) for ts in res.equity.index], "rets": [float(x) for x in rets], "pnls": [float(t["net_pnl"]) for t in res.trades]})
    if intraday:
        out["exits"] = dict(Counter(t["exit_reason"] for t in res.trades))
        out["overnight"] = int(sum(1 for t in res.trades if t["entry_ts"].tz_convert("Europe/Stockholm").date() != t["exit_ts"].tz_convert("Europe/Stockholm").date()))
        out["financing"] = float(sum(t.get("financing", 0.0) for t in res.trades))
        out["cost_paid"] = float(sum(t["fees"] + t["spread_slippage_cost"] for t in res.trades))
    return out


def _eval_task(task):
    cell, params, iid = task
    bars = _G["bars"][iid]
    cfg = SimConfig.from_dict(_G["cfgs"][iid])
    out = {"cell": cell, "iid": iid, "folds": []}
    try:
        for f in _G["folds"][iid]:
            tr = Simulator(cfg).run(bars, build_strategy(_G["strategy"], params), trade_start=f["train_start"], trade_end=f["train_end"])
            te = Simulator(cfg).run(bars, build_strategy(_G["strategy"], params), trade_start=f["test_start"], trade_end=f["test_end"])
            out["folds"].append({"fold": f["index"], "train": _segment(tr, cfg.capital, _G["interval"], keep_series=False), "test": _segment(te, cfg.capital, _G["interval"])})
    except Exception as exc:       # a broken cell (e.g. sandbox violation) is excluded but still counts as a trial
        out["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    return out


# ---------------------------------------------------------------- run
def _f(x, nd=6):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), nd)


def _calendar_folds(dev: dict, spec) -> dict:
    """Folds defined on a common weekday calendar and mapped to each market's bars by DATE. A market takes part in a fold only if its history covers
    the training and test window (so a late-starting market never lends later information to an earlier fold of another market)."""
    first = min(d.index[0].normalize() for d in dev.values()).tz_localize(None)
    last = max(d.index[-1].normalize() for d in dev.values()).tz_localize(None)
    ref = pd.bdate_range(first, last).tz_localize("UTC")
    rfolds = make_folds(len(ref), spec.train_bars, spec.test_bars, step=spec.step, embargo=spec.embargo, anchored=spec.anchored)
    bound = lambda k: ref[k] if k < len(ref) else ref[-1] + pd.Timedelta(days=1)
    out = {i: [] for i in dev}
    for f in rfolds:
        b_ts, b_te, b_xs, b_xe = bound(f.train_start), bound(f.train_end), bound(f.test_start), bound(f.test_end)
        for i, d in dev.items():
            norm = d.index.normalize()
            its, ite, ixs, ixe = (int(norm.searchsorted(b, side="left")) for b in (b_ts, b_te, b_xs, b_xe))
            covers = (norm[0] <= b_ts + 0.15 * (b_te - b_ts) and norm[-1] >= b_xe - pd.Timedelta(days=7)
                      and ite - its >= 0.6 * spec.train_bars and ixe - ixs >= 0.6 * spec.test_bars)
            if covers:
                out[i].append({"index": f.index, "train_start": its, "train_end": ite, "test_start": ixs, "test_end": ixe})
    return out


def _time_folds(dev: dict, spec) -> dict:
    """Folds for intraday series: windows in CALENDAR days on a common calendar, mapped to every market's bars by TIMESTAMP. As with date folds, a market takes
    part in a fold only if its history covers the training and test window, so one market never lends later information to another."""
    first = min(d.index[0].normalize() for d in dev.values()).tz_localize(None)
    last = max(d.index[-1].normalize() for d in dev.values()).tz_localize(None)
    ref = pd.date_range(first, last, freq="D").tz_localize("UTC")
    rfolds = make_folds(len(ref), spec.train_days, spec.test_days, step=spec.step_days, embargo=spec.embargo, anchored=spec.anchored, min_len=5)
    bound = lambda k: ref[k] if k < len(ref) else ref[-1] + pd.Timedelta(days=1)
    out = {i: [] for i in dev}
    for f in rfolds:
        b_ts, b_te, b_xs, b_xe = bound(f.train_start), bound(f.train_end), bound(f.test_start), bound(f.test_end)
        for i, d in dev.items():
            its, ite, ixs, ixe = (int(d.index.searchsorted(b, side="left")) for b in (b_ts, b_te, b_xs, b_xe))
            covers = (d.index[0] <= b_ts + 0.15 * (b_te - b_ts) and d.index[-1] >= b_xe - pd.Timedelta(days=3)
                      and ite - its >= 20 and ixe - ixs >= 20)
            if covers:
                out[i].append({"index": f.index, "train_start": its, "train_end": ite, "test_start": ixs, "test_end": ixe})
    return out


def run_study(spec: StudySpec, bars_by_instrument: dict, dev_end: dict, *, workers: int = 1, quality: dict | None = None) -> dict:
    """bars_by_instrument: id -> DataFrame (full history); dev_end: id -> first index NOT usable (absolute, into the frame as given).
    Nothing at/after dev_end is passed on. Data before `start_date` / the usable-from date is clipped, glitch bars are removed (if clean_data)."""
    start = pd.Timestamp(spec.start_date, tz="UTC") if spec.start_date else None
    dev, excluded, removed = {}, {}, {}
    for i in spec.instruments:
        full = bars_by_instrument[i].iloc[:dev_end[i]]
        lo_date = start
        if spec.respect_usable_from and usable_from(i) and not is_intraday(spec.interval):      # usable-from dates describe the DAILY history
            uf = pd.Timestamp(usable_from(i), tz="UTC")
            lo_date = uf if lo_date is None else max(lo_date, uf)
        d = full if lo_date is None else full.loc[full.index >= lo_date]
        if spec.clean_data:
            d, rem = clean_bars(d)
            if rem:
                removed[i] = rem
        if spec.fold_mode == "days":
            if len(d) < 40 or (d.index[-1] - d.index[0]).days < spec.train_days + spec.test_days:
                excluded[i] = f"history of {0 if len(d) < 2 else (d.index[-1] - d.index[0]).days} days (< train+test = {spec.train_days + spec.test_days})"
                continue
        elif len(d) < spec.train_bars + spec.test_bars:
            excluded[i] = f"only {len(d)} usable bars (< train+test = {spec.train_bars + spec.test_bars})"
            continue
        dev[i] = d
    if not dev:
        raise ValueError("no market has enough usable history: " + json.dumps(excluded))
    specs = {i: load_instrument(i) for i in dev}
    if spec.fold_mode == "dates":
        folds = _calendar_folds(dev, spec)
    elif spec.fold_mode == "days":
        folds = _time_folds(dev, spec)
    else:
        folds = {i: [vars(f) for f in make_folds(len(dev[i]), spec.train_bars, spec.test_bars, step=spec.step, embargo=spec.embargo, anchored=spec.anchored)] for i in dev}
    for i in list(dev):
        if not folds[i]:
            excluded[i] = "its history covers no complete training+test window"
            dev.pop(i), folds.pop(i), specs.pop(i)
    if not dev:
        raise ValueError("no market takes part in any fold: " + json.dumps(excluded))
    ids = list(dev)
    cells = spec.cells()
    stress = StressModel(spread_multiplier=spec.cost_multiplier, slippage_multiplier=spec.cost_multiplier)
    intraday = is_intraday(spec.interval)
    rules = SessionRules.from_policy(interval_minutes(spec.interval)) if intraday and not spec.allow_overnight else None      # Saba's intraday-only policy
    icosts = {i: (intraday_costs(i) if intraday and spec.intraday_costs else None) for i in ids}
    payload = {"bars": dev, "strategy": spec.strategy, "interval": spec.interval,
               "cfgs": {i: SimConfig(spec.capital, None, None, instrument=specs[i], stress=stress, session=rules, intraday_costs=icosts[i]).to_dict() for i in ids},
               "folds": {i: folds[i] for i in ids}}
    tasks = [(c, p, i) for c, p in enumerate(cells) for i in ids]
    if workers > 1:
        with cf.ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(payload,)) as ex:
            results = list(ex.map(_eval_task, tasks, chunksize=max(1, len(tasks) // (workers * 4))))
    else:
        _init(payload)
        results = [_eval_task(t) for t in tasks]
    out = _aggregate(spec, cells, ids, specs, dev, folds, results)
    out["excluded_markets"] = excluded
    out["glitch_bars_removed"] = {i: [x["date"] for x in v] for i, v in removed.items()}
    out["data_quality"] = quality or {}
    if intraday:
        out["intraday"] = {"allow_overnight": bool(spec.allow_overnight), "session_policy": rules.to_dict() if rules else None,
                           "cost_multiplier": spec.cost_multiplier,
                           "cost_profiles": {i: (icosts[i].source if icosts[i] else "flat bps (no intraday profile)") for i in ids}}
    return out


def _aggregate(spec, cells, ids, specs, dev, folds, results):
    n_fold = {i: len(folds[i]) for i in ids}
    res = {(r["cell"], r["iid"]): r for r in results}
    valid = [c for c in range(len(cells)) if all("error" not in res[(c, i)] for i in ids)]
    errors = {c: next(res[(c, i)]["error"] for i in ids if "error" in res[(c, i)]) for c in range(len(cells)) if c not in valid}
    if not valid:       # a study in which every cell crashed is a bug or a bad spec, never a "no evidence" result: do not record it
        raise ValueError(f"every cell of the study failed; first error: {next(iter(errors.values()), 'unknown')}")
    bpy = float(np.mean([specs[i].bars_per_year(spec.interval) for i in ids]))     # legacy ("fixed" pooling); "active" pooling re-derives it from the dates below
    n_inst = len(ids)
    fixed = spec.pooling == "fixed"
    fold_ids = sorted({f['index'] for i in ids for f in folds[i]})

    def seg(c, i, fi, which):
        for s in res[(c, i)]["folds"]:
            if s["fold"] == fi:
                return s[which]
        return None

    def series(parts):
        """parts: list of test segments -> {date: portfolio return} (equal weight, absent market = 0)"""
        acc, cnt = {}, {}
        for s in parts:
            for d, r in zip(s["dates"], s["rets"]):
                acc[d] = acc.get(d, 0.0) + r
                cnt[d] = cnt.get(d, 0) + 1
        return {d: acc[d] / (n_inst if fixed else cnt[d]) for d in acc}

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
    if not fixed and len(w_dates) > 2:
        yrs = (pd.Timestamp(w_dates[-1]) - pd.Timestamp(w_dates[0])).days / 365.25
        bpy = len(w_dates) / yrs if yrs > 0 else bpy
    oos_years = (pd.Timestamp(w_dates[-1]) - pd.Timestamp(w_dates[0])).days / 365.25 if len(w_dates) > 1 else 0.0
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
    active_days = {i: {d for fi in fold_ids if valid and seg(valid[0], i, fi, "test") for d in seg(valid[0], i, fi, "test")["dates"]} for i in ids}
    n_active = np.array([sum(1 for i in ids if d in active_days[i]) for d in w_dates], dtype=float)
    for i in ids:
        cl = dev[i]["Close"].pct_change().fillna(0.0)
        cl.index = [_key(t, is_intraday(spec.interval)) for t in cl.index]
        bh += np.array([float(cl.get(d, 0.0)) if d in active_days[i] else 0.0 for d in w_dates]) / (n_inst if fixed else np.maximum(n_active, 1.0))
    exp_ci = bootstrap_mean_ci(wfo_pnls)
    folds_with_trades = [r for r in fold_rows if r.get("oos_return_pct") is not None]
    pos_folds = float(np.mean([r["oos_return_pct"] > 0 for r in folds_with_trades])) if folds_with_trades else 0.0
    pos_markets = int(sum(v > 0 for v in wfo_inst_ret.values()))
    wins = sum(p > 0 for p in wfo_pnls)
    wfo = {"oos_days": len(w), "oos_years": _f(oos_years, 2), "sharpe_annualized": _f(sr_w * math.sqrt(bpy), 3), "return_pct": _f(float(np.prod(1 + w) - 1) * 100, 3) if len(w) else None,
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
    if is_intraday(spec.interval):
        sel = [p for p in wfo_parts]
        wfo["exit_reasons"] = dict(sum((Counter(p.get("exits", {})) for p in sel), Counter()))
        wfo["trades_held_overnight"] = int(sum(p.get("overnight", 0) for p in sel))
        wfo["financing_paid"] = _f(sum(p.get("financing", 0.0) for p in sel), 2)
        wfo["spread_slippage_and_fees_paid"] = _f(sum(p.get("cost_paid", 0.0) for p in sel), 2)
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
            "markets": ids, "folds": {i: n_fold[i] for i in ids}, "fold_windows": len(fold_ids), "data_used": {i: f"bars 0..{len(dev[i]) - 1} ({dev[i].index[0].date()}..{dev[i].index[-1].date()})" for i in ids},
            "wfo": wfo, "best_single_cell": best_info, "pbo": pbo, "benchmarks": bench, "verdict": "candidate" if not reasons else "no_evidence",
            "failed_criteria": reasons, "thresholds": THRESHOLDS, "fold_table": fold_rows, "cell_table": cell_rows,
            "pooling": spec.pooling, "fold_mode": spec.fold_mode, "bars_per_year_used": _f(bpy, 1), "cost_multiplier": spec.cost_multiplier,
            "note": "Candidate = worth freezing and tracking forward, not proven. No_evidence = the search procedure did not beat what noise would produce."}


# ---------------------------------------------------------------- registry integration
def run_recorded_study(registry, archive, spec: StudySpec, *, workers: int = 1, before_run=None) -> dict:
    """Loads archived bars, respects every market's consumed-test frontier / protected tail, deduplicates, runs and records the study.
    Trials already spent on these markets (the largest per-market count) raise the deflation hurdle, and every cell of the study is added to the trial counts.
    `before_run` (optional callable) is invoked only when the study will really be computed and recorded, never for a reused (identical) study,
    so that budgets are charged once per new piece of research."""
    from .integrity import SpecIndex, code_version, dataset_sha256, market_key, test_frontier
    from .walkforward import development_end
    bars, dev_end, quality = {}, {}, {}
    for i in spec.instruments:
        s = load_instrument(i)
        full = archive.read(s.symbol, spec.interval)
        bars[i] = full
        lo_date = pd.Timestamp(spec.start_date, tz="UTC") if spec.start_date else None
        if spec.respect_usable_from and usable_from(i) and not is_intraday(spec.interval):
            uf = pd.Timestamp(usable_from(i), tz="UTC")
            lo_date = uf if lo_date is None else max(lo_date, uf)
        lo = 0 if lo_date is None else int(full.index.searchsorted(lo_date))
        window = full.iloc[lo:]
        # development end = 80 % of the ANALYSED window (or the consumed-test frontier); the remaining tail stays untouched
        dev_end[i] = lo + development_end(window, test_frontier(registry, s.symbol, spec.interval))
        analysed = clean_bars(full.iloc[lo:dev_end[i]])[0] if spec.clean_data else full.iloc[lo:dev_end[i]]
        q = check_bars(analysed, interval=spec.interval, asset_class=s.asset_class, min_bars=100 if spec.fold_mode == "days" else spec.train_bars + spec.test_bars)
        quality[i] = {"status": q["status"], "bars": q["bars"], "first": q.get("first"), "last": q.get("last"), "findings": [f["message"] for f in q["findings"]]}
        if q["status"] == "fail":
            raise ValueError(f"data quality check failed for {i}: " + "; ".join(f["message"] for f in q["findings"] if f["level"] == "fail"))
        if is_intraday(spec.interval):          # the series must match the market's session calendar (24/7, 24/5, Globex break, regular hours)
            kind = session_kind(i)
            if kind is None:
                quality[i]["session"] = {"status": "unchecked", "note": "no session calendar for this instrument in data/intraday_profiles.json"}
            else:
                sq = check_session(analysed, kind, spec.interval)
                quality[i]["session"] = {k: sq[k] for k in ("status", "kind", "completeness", "missing_expected_bars", "longest_missing_run_bars", "outside_session_bars")}
                quality[i]["session"]["findings"] = [f["message"] for f in sq["findings"]]
                if sq["status"] == "fail":
                    raise ValueError(f"session check failed for {i}: " + "; ".join(f["message"] for f in sq["findings"]))
    dev_sha = {i: dataset_sha256(bars[i].iloc[:dev_end[i]]) for i in spec.instruments}
    study_sha = hashlib.sha256(json.dumps({"spec": spec.to_dict(), "dev": dev_sha, "code": code_version()}, sort_keys=True, default=str).encode()).hexdigest()
    for rid, cfg_json, res_json in registry.db.execute("SELECT id, configuration_json, results_json FROM experiments WHERE name='academy:study'"):
        if json.loads(cfg_json).get("study_sha256") == study_sha:
            return {**json.loads(res_json), "reused": True, "registry_id": rid}
    if before_run is not None:
        before_run()
    index = SpecIndex(registry)
    markets = {i: market_key(load_instrument(i).symbol, spec.interval) for i in spec.instruments}
    # one pooled series per cell: trials already spent on the markets raise the hurdle by the largest per-market count (not their sum)
    prior = max([spec.prior_trials] + [index.trials(m) for m in set(markets.values())])
    eff = StudySpec(**{**spec.to_dict(), "prior_trials": prior})
    result = run_study(eff, bars, dev_end, workers=workers, quality=quality)
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
