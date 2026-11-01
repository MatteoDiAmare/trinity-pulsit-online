#!/usr/bin/env python3
"""Runs the same three strategy-family studies under several data designs, each in its own empty registry (so trial counts are comparable)."""
import json, os, shutil, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_data.archive import MarketArchive
from market_data.laboratory import ExperimentRegistry
from market_data.academy.study import StudySpec, run_recorded_study
from market_data.academy.instruments import available_instruments, load_instrument

OLD = Path.home() / ".local/share/saba/market_data"
EXT = Path.home() / ".local/share/saba/market_data_extended"
FAMILIES = {
    "sma": ("sma_cross", {"stop_loss_pct": 2.0}, {"short": [5, 10, 20], "long": [40, 80, 150]}),
    "breakout": ("breakout", {"stop_loss_pct": 2.0}, {"window": [10, 20, 40, 80], "stop_loss_pct": [1.0, 2.0, 3.0]}),
    "mr": ("mean_reversion", {"stop_loss_pct": 2.0}, {"window": [10, 20, 40], "z_entry": [1.5, 2.0, 2.5]}),
}
THREE = ["GOLD", "EURUSD", "SP500"]
ALL16 = sorted(i for i in available_instruments())
DESIGNS = {"A_3mkts_10y_newmethod": (OLD, THREE), "B_3mkts_full": (EXT, THREE), "C_16mkts_full": (EXT, ALL16)}


def run(design, family, workers):
    src, ids = DESIGNS[design]
    tmp = Path(tempfile.mkdtemp(prefix="breadth-"))
    for i in ids:
        sym = load_instrument(i).symbol
        os.symlink(src / sym, tmp / sym)
    reg = ExperimentRegistry(tmp / "experiments.sqlite3")
    strat, base, grid = FAMILIES[family]
    spec = StudySpec(name=f"{family} {design}", strategy=strat, base_params=base, grid=grid, instruments=ids, interval="1d", train_bars=750, test_bars=125)
    t = time.time()
    out = run_recorded_study(reg, MarketArchive(tmp), spec, workers=workers)
    out["seconds"] = round(time.time() - t, 1)
    reg.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return out


if __name__ == "__main__":
    outdir = Path(sys.argv[1])
    outdir.mkdir(parents=True, exist_ok=True)
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    for design in DESIGNS:
        for family in FAMILIES:
            f = outdir / f"{design}__{family}.json"
            if f.exists():
                continue
            r = run(design, family, workers)
            json.dump(r, open(f, "w"), default=str)
            w = r["wfo"]
            print(f"{design:24s} {family:9s} markets {len(r['markets']):2d} folds {r['fold_windows']:3d} oos_years {w['oos_years']} trades {w['trades']:5d} sharpe {w['sharpe_annualized']} dsr {w['dsr']} verdict {r['verdict']} ({r['seconds']}s)", flush=True)
