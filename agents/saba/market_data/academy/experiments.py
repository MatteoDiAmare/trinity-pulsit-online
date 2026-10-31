"""Experiments: chronological splits, seeded parameter search, registry integration.

Protocol
* Bars are split chronologically into train / validation / final test.
* `run_training` only ever receives bars up to the end of validation (the final test
  slice is not even hashed or loaded into the simulator) and selects by VALIDATION score.
* `run_final_test` evaluates one already-selected experiment on the untouched test slice,
  once per experiment (guarded through the registry).
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import numpy as np
import pandas as pd

from ..laboratory import ExperimentRegistry
from .evaluation import compute_metrics
from .execution import CostModel
from .integrity import (FINAL_TESTS_PER_WINDOW, MIN_FRESH_TEST_BARS, SpecIndex, code_version, dataset_sha256,
                        expected_max_noise_sharpe, final_tests, market_key, spec_hash, test_frontier, window_id)
from .ledger import LedgerStore, result_payload
from .simulator import SimConfig, Simulator
from .strategies import STRATEGIES, build_strategy

SPLIT_VERSION = 1


@dataclass(frozen=True)
class Split:
    train_end: int   # exclusive bar index
    val_end: int     # exclusive bar index; bars from here on are the final test

    @staticmethod
    def chronological(n: int, train: float = 0.6, validation: float = 0.2) -> "Split":
        if not (0 < train and 0 < validation and train + validation < 1):
            raise ValueError("fractions must be positive and leave room for a final test")
        a, b = int(n * train), int(n * (train + validation))
        if a < 50 or b - a < 20 or n - b < 20:
            raise ValueError("not enough bars for train/validation/test split")
        return Split(a, b)

    @staticmethod
    def after_frontier(bars: pd.DataFrame, frontier) -> "Split":
        """Development = everything up to the last consumed final-test bar; the new final test = fresh bars after it."""
        n_dev = int((bars.index <= frontier).sum())
        fresh = len(bars) - n_dev
        if fresh < MIN_FRESH_TEST_BARS:
            raise ValueError(f"no fresh final-test window: only {fresh} bars after the last consumed test window "
                             f"({frontier.isoformat()}); at least {MIN_FRESH_TEST_BARS} are needed - wait for more data")
        a = int(n_dev * 0.75)
        if a < 50 or n_dev - a < 20:
            raise ValueError("not enough development bars")
        return Split(a, n_dev)


def sample_params(name: str, rng: np.random.Generator, seed: int) -> dict:
    """Random-search parameter sampler; the ONLY randomness is the passed generator."""
    sl = [0.5, 1.0, 2.0, 3.0][int(rng.integers(0, 4))]      # stop-loss is mandatory
    rng.integers(0, 4)                                    # keeps the draw sequence aligned
    tp = [None, 1.0, 2.0, 4.0][int(rng.integers(0, 4))]
    short = bool(rng.integers(0, 2))
    if name == "sma_cross":
        s = int(rng.integers(3, 40))
        return {"short": s, "long": s + int(rng.integers(5, 120)), "stop_loss_pct": sl,
                "take_profit_pct": tp, "allow_short": short}
    if name == "breakout":
        return {"window": int(rng.integers(5, 100)), "stop_loss_pct": sl, "take_profit_pct": tp,
                "allow_short": short}
    if name == "mean_reversion":
        return {"window": int(rng.integers(5, 100)), "z_entry": float(round(rng.uniform(1.0, 3.0), 2)),
                "stop_loss_pct": sl, "take_profit_pct": tp, "allow_short": short}
    if name == "random_control":
        return {"seed": seed, "trade_prob": float(round(rng.uniform(0.005, 0.05), 4)),
                "hold_bars": int(rng.integers(3, 30)), "stop_loss_pct": sl, "take_profit_pct": tp}
    raise ValueError(name)


def plan_experiments(n: int, seed: int) -> list[dict]:
    names = sorted(STRATEGIES)
    rng = np.random.default_rng(seed)
    return [{"strategy": names[k % len(names)],
             "params": sample_params(names[k % len(names)], rng, seed * 1_000_003 + k)} for k in range(n)]


def evaluate_segment(bars, spec: dict, sim_config: SimConfig, start: int, end: int, interval: str,
                     *, keep_result: bool = False):
    result = Simulator(sim_config).run(bars, build_strategy(spec["strategy"], spec["params"]),
                                       trade_start=start, trade_end=end)
    metrics = compute_metrics(result, interval)
    return (metrics, result) if keep_result else metrics


def selection_score(m: dict) -> float:
    """Validation-only score: annualized Sharpe, with trade-count and ruin guards."""
    if m["trades"] < 5 or m["halted"]:
        return float("-inf")
    return m["sharpe_annualized"] or 0.0


def _config_dict(symbol, interval, spec, sim_config, split, seed, bars):
    return {"symbol": symbol, "interval": interval, "strategy": spec["strategy"], "params": spec["params"],
            "seed": seed, "capital": sim_config.capital, "costs": sim_config.effective_costs.as_dict(),
            "max_exposure": sim_config.max_exposure, "allow_short": sim_config.allow_short,
            "sim_config": sim_config.to_dict(),
            "instrument_id": sim_config.instrument.id if sim_config.instrument else None,
            "instrument_spec_sha256": sim_config.instrument.spec_sha256 if sim_config.instrument else None,
            "split": {"version": SPLIT_VERSION, "train_end_index": split.train_end,
                      "val_end_index": split.val_end,
                      "train_start_utc": bars.index[0].isoformat(),
                      "val_start_utc": bars.index[split.train_end].isoformat(),
                      "test_start_utc": bars.index[split.val_end].isoformat() if split.val_end < len(bars) else None,
                      "dev_end_utc": bars.index[split.val_end - 1].isoformat()}}


def run_training(bars: pd.DataFrame, registry: ExperimentRegistry, *, symbol: str, interval: str,
                 experiments: int = 100, seed: int = 0, sim_config: SimConfig | None = None,
                 split: Split | None = None, control_runs: int = 30, ledger: LedgerStore | None = None) -> dict:
    sim_config = sim_config or SimConfig()
    if split is None:
        frontier = test_frontier(registry, symbol, interval)
        split = Split.after_frontier(bars, frontier) if frontier is not None else Split.chronological(len(bars))
    dev = bars.iloc[:split.val_end]          # final test slice never leaves this function
    index, market, version = SpecIndex(registry), market_key(symbol, interval), code_version()
    dev_sha = dataset_sha256(dev)
    split_key = {"train_end": split.train_end, "val_end": split.val_end, "version": SPLIT_VERSION}
    rows, reused, new_trials = [], 0, 0
    for k, spec in enumerate(plan_experiments(experiments, seed)):
        h = spec_hash(spec["strategy"], spec["params"], sim_config.to_dict(), dev_sha, split_key, version)
        known = index.lookup(h)
        if known is not None:     # identical experiment already done: reuse, never re-count
            old = json.loads(registry.db.execute("SELECT results_json FROM experiments WHERE id=?", (known,)).fetchone()[0])
            reused += 1
            rows.append({"experiment_id": known, "strategy": spec["strategy"], "params": spec["params"],
                         "score": selection_score(old["validation"]), "train": old["train"],
                         "validation": old["validation"], "reused": True})
            continue
        train_m, train_r = evaluate_segment(dev, spec, sim_config, 0, split.train_end, interval, keep_result=True)
        val_m, val_r = evaluate_segment(dev, spec, sim_config, split.train_end, split.val_end, interval, keep_result=True)
        score = selection_score(val_m)
        new_trials += 1
        config = _config_dict(symbol, interval, spec, sim_config, split, seed, bars)
        config.update({"spec_sha256": h, "code_version": version, "dev_dataset_sha256": dev_sha})
        results = {"train": train_m, "validation": val_m, "trial_number_on_market": index.trials(market) + 1,
                   "validation_score": score if np.isfinite(score) else None, "experiment_index": k}
        exp_id = registry.record(f"academy:{spec['strategy']}", dev, config, results)
        index.add(h, exp_id, market)
        if ledger is not None:
            ledger.write_meta(exp_id, {"kind": "training_experiment", "symbol": symbol, "interval": interval,
                                       "strategy": spec["strategy"], "params": spec["params"],
                                       "configuration": config, "results": results})
            ledger.write_period(exp_id, "train", result_payload(train_r, train_m))
            ledger.write_period(exp_id, "validation", result_payload(val_r, val_m))
        rows.append({"experiment_id": exp_id, "strategy": spec["strategy"], "params": spec["params"],
                     "score": score, "train": train_m, "validation": val_m})
    controls = []
    for j in range(control_runs):
        spec = {"strategy": "random_control", "params": {"seed": seed * 7919 + j, "trade_prob": 0.02, "hold_bars": 10}}
        controls.append(evaluate_segment(dev, spec, sim_config, split.train_end, split.val_end, interval)["net_return_pct"])
    ranked = sorted(rows, key=lambda r: (-r["score"], r["experiment_id"]))
    best = ranked[0] if ranked and np.isfinite(ranked[0]["score"]) else None
    total_trials = index.trials(market)
    bpy = sim_config.instrument.bars_per_year(interval) if sim_config.instrument else 252.0
    noise = expected_max_noise_sharpe(total_trials, split.val_end - split.train_end, bpy)
    summary = {
        "symbol": symbol, "interval": interval, "seed": seed, "experiments": experiments,
        "split": _config_dict(symbol, interval, plan_experiments(1, 0)[0], sim_config, split, seed, bars)["split"],
        "selected_experiment_id": best["experiment_id"] if best else None,
        "new_trials": new_trials, "reused_experiments": reused, "trials_total_on_market": total_trials,
        "noise_benchmark_sharpe": noise,
        "selected_exceeds_noise_benchmark": None if best is None else bool((best["validation"]["sharpe_annualized"] or 0) > noise),
        "code_version": version, "dev_dataset_sha256": dev_sha,
        "selected": None if best is None else {k: best[k] for k in ("strategy", "params", "train", "validation")},
        "random_control_validation_net_return_pct": {
            "runs": control_runs,
            "mean": float(np.mean(controls)) if controls else None,
            "p95": float(np.percentile(controls, 95)) if controls else None,
            "selected_beats_p95": None if best is None or not controls
            else bool(best["validation"]["net_return_pct"] > np.percentile(controls, 95))},
        "top5": [{"experiment_id": r["experiment_id"], "strategy": r["strategy"], "score": r["score"],
                  "validation_net_return_pct": r["validation"]["net_return_pct"]} for r in ranked[:5]],
        "warning": ("Selected from %d trials on validation data: expect selection bias. "
                    "Confirm once on the untouched final test." % experiments),
    }
    summary_id = registry.record("academy:train_summary", dev,
                                 {"symbol": symbol, "interval": interval, "seed": seed, "experiments": experiments},
                                 _jsonable(summary))
    summary["summary_experiment_id"] = summary_id
    if ledger is not None:
        ledger.write_summary(summary_id, _jsonable(summary))
    return summary


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    return obj


def run_final_test(bars: pd.DataFrame, registry: ExperimentRegistry, experiment_id: int, *,
                   keep_result: bool = False, ledger: LedgerStore | None = None) -> dict:
    """Confirm one selected experiment on its untouched test window. No override exists:
    an experiment is tested once, a window confirms at most FINAL_TESTS_PER_WINDOW experiments,
    and a window overlapping an already consumed one is refused."""
    row = registry.db.execute("SELECT name,configuration_json,results_json FROM experiments WHERE id=?",
                              (experiment_id,)).fetchone()
    if row is None or not row[0].startswith("academy:") or "params" not in json.loads(row[1]):
        raise ValueError("experiment_id is not an Academy training experiment")
    rec = {"name": row[0], "configuration": json.loads(row[1]), "results": json.loads(row[2])}
    cfg = rec["configuration"]
    symbol, interval = cfg["symbol"], cfg["interval"]
    if ledger is not None and experiment_id not in ledger.experiments():
        raise ValueError("experiment has no ledger for this owner/org (check --owner-id/--org-id)")
    split = Split(cfg["split"]["train_end_index"], cfg["split"]["val_end_index"])
    if len(bars) < split.val_end or bars.index[split.val_end - 1].isoformat() != cfg["split"]["dev_end_utc"]:
        raise ValueError("archive no longer matches the recorded split (history changed)")
    if len(bars) - split.val_end < MIN_FRESH_TEST_BARS:
        raise ValueError("test window has too few bars yet; wait for more data")
    test_start = bars.index[split.val_end]
    wid = window_id(symbol, interval, test_start.isoformat())
    consumed = final_tests(registry, symbol, interval)
    if any(c["experiment_id"] == experiment_id for c in consumed):
        raise ValueError("final test already consumed for this experiment; it cannot be repeated")
    same = [c for c in consumed if window_id(symbol, interval, c["test_start_utc"]) == wid]
    if len(same) >= FINAL_TESTS_PER_WINDOW:
        raise ValueError(f"test window exhausted: {len(same)} experiments were already confirmed on it; "
                         "train again once fresh data has arrived")
    if any(test_start <= pd.Timestamp(c["test_end_utc"]) and window_id(symbol, interval, c["test_start_utc"]) != wid
           for c in consumed):
        raise ValueError("this experiment's test window overlaps a window consumed by another test; "
                         "its bars are development data now - retrain")
    sim_config = SimConfig.from_dict(cfg["sim_config"]) if "sim_config" in cfg else SimConfig(
        cfg["capital"], CostModel(**cfg["costs"]), cfg["max_exposure"], allow_short=cfg["allow_short"])
    spec = {"strategy": cfg["strategy"], "params": cfg["params"]}
    metrics, sim = evaluate_segment(bars, spec, sim_config, split.val_end, len(bars), interval, keep_result=True)
    result = sim if keep_result else None
    payload = {"final_test": metrics, "validation": rec["results"]["validation"], "window_id": wid,
               "tests_on_window_including_this": len(same) + 1, "window_budget": FINAL_TESTS_PER_WINDOW}
    test_cfg = {"experiment_id": experiment_id, "symbol": symbol, "interval": interval, "window_id": wid,
                "test_start_utc": test_start.isoformat(), "test_end_utc": bars.index[-1].isoformat(),
                "strategy": cfg["strategy"], "params": cfg["params"], "code_version": code_version(),
                "spec_sha256": cfg.get("spec_sha256")}
    payload["final_test_experiment_id"] = registry.record("academy:final_test", bars.iloc[split.val_end:], test_cfg,
                                                          _jsonable({"final_test": metrics}))
    if ledger is not None:
        ledger.write_period(experiment_id, "final_test", result_payload(sim, metrics))
    return (payload, result) if keep_result else payload
