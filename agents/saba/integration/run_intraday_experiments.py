#!/usr/bin/env python3
"""Intraday walk-forward studies on an intraday archive, each run in its own EMPTY registry (so trial counts are comparable between variants).

  python3 integration/run_intraday_experiments.py ARCHIVE_ROOT OUTDIR [--instruments BTCUSD_BITSTAMP] [--interval 60m] [--workers 2] [--only sma:policy_x1,...]

Variants (names): policy_x1 / policy_x2 = Saba's intraday-only policy (no carried positions) at normal / double spread+slippage;
overnight_x1 / overnight_x2 = the same with `allow_overnight: true` stated explicitly. Families are scaled to hourly bars.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_data.academy.instruments import load_instrument
from market_data.academy.study import StudySpec, run_recorded_study
from market_data.archive import MarketArchive
from market_data.laboratory import ExperimentRegistry

FAMILIES = {
    "sma": ("sma_cross", {"stop_loss_pct": 1.5}, {"short": [6, 12, 24], "long": [48, 96, 240]}),
    "breakout": ("breakout", {"stop_loss_pct": 1.5}, {"window": [24, 48, 96], "stop_loss_pct": [0.8, 1.5, 2.5]}),
    "mr": ("mean_reversion", {"stop_loss_pct": 1.5}, {"window": [24, 48, 96], "z_entry": [1.5, 2.0, 2.5]}),
}
VARIANTS = {"policy_x1": dict(allow_overnight=False, cost_multiplier=1.0), "policy_x2": dict(allow_overnight=False, cost_multiplier=2.0),
            "overnight_x1": dict(allow_overnight=True, cost_multiplier=1.0), "overnight_x2": dict(allow_overnight=True, cost_multiplier=2.0)}


def run(root, ids, interval, family, variant, workers, train_days, test_days):
    tmp = Path(tempfile.mkdtemp(prefix="intraday-"))
    for i in ids:
        sym = load_instrument(i).symbol
        os.symlink(Path(root) / sym, tmp / sym)
    reg = ExperimentRegistry(tmp / "experiments.sqlite3")
    strat, base, grid = FAMILIES[family]
    spec = StudySpec(name=f"{family} {variant} {interval}", strategy=strat, base_params=base, grid=grid, instruments=ids, interval=interval,
                     fold_mode="days", train_days=train_days, test_days=test_days, **VARIANTS[variant])
    t = time.time()
    out = run_recorded_study(reg, MarketArchive(tmp), spec, workers=workers)
    out["seconds"] = round(time.time() - t, 1)
    reg.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("root"), ap.add_argument("outdir")
    ap.add_argument("--instruments", default="BTCUSD_BITSTAMP")
    ap.add_argument("--interval", default="60m")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--train-days", type=int, default=180)
    ap.add_argument("--test-days", type=int, default=30)
    ap.add_argument("--only", default=",".join(f"{f}:{v}" for f in FAMILIES for v in ("policy_x1", "policy_x2")) + ",sma:overnight_x1,sma:overnight_x2")
    a = ap.parse_args()
    Path(a.outdir).mkdir(parents=True, exist_ok=True)
    for item in a.only.split(","):
        fam, var = item.split(":")
        f = Path(a.outdir) / f"{a.interval}__{'+'.join(a.instruments.split(','))}__{fam}__{var}.json"
        if f.exists():
            continue
        r = run(a.root, a.instruments.split(","), a.interval, fam, var, a.workers, a.train_days, a.test_days)
        json.dump(r, open(f, "w"), default=str)
        w = r["wfo"]
        print(f"{fam:9s} {var:13s} folds {r['fold_windows']:3d} oos_years {w['oos_years']} trades {w['trades']:6d} sharpe {w['sharpe_annualized']} dsr {w['dsr']} "
              f"overnight {w.get('trades_held_overnight')} verdict {r['verdict']} ({r['seconds']}s)", flush=True)
