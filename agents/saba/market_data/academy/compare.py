"""Cross-market comparison: run the same strategies on several instruments and compare the evidence.

Every (strategy, market) cell goes through the normal training path, so it is recorded, deduplicated and
COUNTED as a trial on that market (choosing a market is itself a degree of freedom). Only train and
validation data are used here; no final test is touched. A strategy that works on one market only is a
weaker finding than one that transfers unchanged, so the report includes a transfer count.
"""
from __future__ import annotations

import json

from ..archive import MarketArchive
from ..laboratory import ExperimentRegistry
from .experiments import Split, run_training, selection_score
from .instruments import InstrumentSpec
from .integrity import market_key
from .ledger import LedgerStore
from .simulator import SimConfig

METRICS = ("net_return_pct", "max_drawdown_pct", "sharpe_annualized", "return_over_max_drawdown", "trades",
           "hit_rate", "avg_r_multiple", "exposure_fraction", "shortfall", "stop_failures")


def _row(m: dict) -> dict:
    return {k: m.get(k) for k in METRICS} | {"cost_per_trade_pct_of_capital": (
        (m["total_fees"] + m["total_spread_slippage"]) / m["trades"] / m["start_equity"] * 100) if m["trades"] else None}


def compare_markets(registry: ExperimentRegistry, archive: MarketArchive, plan: list[dict],
                    instruments: list[InstrumentSpec], *, interval: str = "1d", capital: float = 1000.0,
                    sim_template: SimConfig | None = None, ledger: LedgerStore | None = None) -> dict:
    """plan: [{strategy, params, label}], identical for every instrument."""
    if len(instruments) < 2:
        raise ValueError("a comparison needs at least two instruments")
    labels = [p.get("label") or f"{p['strategy']}#{i}" for i, p in enumerate(plan)]
    if len(set(labels)) != len(labels):
        raise ValueError("plan labels must be unique")
    cells, skipped = {}, {}
    for spec in instruments:
        try:
            bars = archive.read(spec.symbol, interval)
        except FileNotFoundError:
            skipped[spec.id] = "no archived data; fetch it first"
            continue
        base = sim_template or SimConfig(capital, max_exposure=None)
        cfg = SimConfig(base.capital, None, None, base.min_notional, base.allow_short, spec, base.risk, base.stress, base.seed)
        out = run_training(bars, registry, symbol=spec.symbol, interval=interval, sim_config=cfg, plan=plan,
                           split=Split.chronological(len(bars)), control_runs=0, ledger=ledger)
        cells[spec.id] = {"summary_id": out["summary_experiment_id"], "rejected": out["rejected_strategies"],
                          "trials_total_on_market": out["trials_total_on_market"], "noise_benchmark_sharpe": out["noise_benchmark_sharpe"]}
        cells[spec.id]["rows"] = out["_rows"]
    ids = [i.id for i in instruments if i.id in cells]
    matrix = {}
    for p, label in zip(plan, labels):
        per = {}
        for iid in ids:
            row = next((r for r in cells[iid]["rows"] if r["label"] == label or (r["strategy"] == p["strategy"] and r["params"] == p["params"])), None)
            if row is None:
                per[iid] = {"status": "rejected_by_causality_gate"}
                continue
            val = row["validation"]
            per[iid] = {"status": "ok", "experiment_id": row["experiment_id"], "train": _row(row["train"]), "validation": _row(val),
                        "exceeds_noise": (val["sharpe_annualized"] or 0) > cells[iid]["noise_benchmark_sharpe"] and val["trades"] >= 5}
        oks = [v for v in per.values() if v["status"] == "ok"]
        matrix[label] = {"strategy": p["strategy"], "per_market": per,
                         "markets_positive_validation": sum(1 for v in oks if (v["validation"]["net_return_pct"] or 0) > 0),
                         "markets_exceeding_noise": sum(1 for v in oks if v["exceeds_noise"]),
                         "markets_evaluated": len(oks),
                         "best_market": max(((iid, v["validation"]["sharpe_annualized"] or float("-inf")) for iid, v in per.items()
                                             if v["status"] == "ok"), key=lambda x: x[1], default=(None, None))[0]}
    best_for_market = {}
    for iid in ids:
        scored = [(label, (m["per_market"][iid]["validation"]["sharpe_annualized"] or float("-inf")))
                  for label, m in matrix.items() if m["per_market"][iid]["status"] == "ok"]
        best_for_market[iid] = max(scored, key=lambda x: x[1])[0] if scored else None
    return {"interval": interval, "markets": ids, "skipped_markets": skipped, "strategies": labels,
            "matrix": matrix, "best_strategy_per_market": best_for_market,
            "transfer": {label: f"{m['markets_exceeding_noise']}/{m['markets_evaluated']} markets above the luck benchmark; "
                                f"{m['markets_positive_validation']}/{m['markets_evaluated']} positive" for label, m in matrix.items()},
            "market_data": {iid: {k: cells[iid][k] for k in ("summary_id", "trials_total_on_market", "noise_benchmark_sharpe", "rejected")} for iid in ids},
            "warning": "Validation-period comparison only; picking the best cell is selection on validation. "
                       "Every cell counts as a trial on its market. Confirm a chosen candidate once on its own fresh final-test window."}
