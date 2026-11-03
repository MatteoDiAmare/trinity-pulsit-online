#!/usr/bin/env python3
"""Summarise integration/run_intraday_experiments.py output: one row per run, cost sensitivity, policy evidence and the minimum detectable true Sharpe.

  python3 integration/analyze_intraday.py OUTDIR
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_data.academy.power import min_detectable_sharpe

rows = []
for f in sorted(Path(sys.argv[1]).glob("*.json")):
    if f.name == "batch.json":
        continue
    r = json.load(open(f))
    fam, var = f.stem.split("__")[-2:]
    w, b = r["wfo"], r["benchmarks"]
    cells = [c["oos_sharpe_annualized"] for c in r["cell_table"] if c.get("valid") and c.get("oos_sharpe_annualized") is not None]
    bpy = r["bars_per_year_used"]
    sd_cells = (sum((x - sum(cells) / len(cells)) ** 2 for x in cells) / max(len(cells) - 1, 1)) ** 0.5 if cells else 0.0
    var_bar = (0.5 / math.sqrt(bpy)) ** 2        # ASSUMED spread of cell Sharpes (0.5 annual): the observed spread of strongly negative cells would overstate the luck hurdle
    md = min_detectable_sharpe(w["oos_years"], 1.0, r["n_trials_for_deflation"], var_bar, periods_per_year=bpy)
    rows.append((fam, var, r, w, b, md))
print(f"{'family':9}{'variant':14}{'OOS yrs':>8}{'trades':>8}{'hit':>6}{'expect':>8}{'return%':>10}{'Sharpe':>8}{'DSR':>6}{'cost paid':>10}{'financing':>10}{'overnight':>10}  exits")
for fam, var, r, w, b, md in rows:
    print(f"{fam:9}{var:14}{w['oos_years']:>8}{w['trades']:>8}{(w['hit_rate'] or 0):>6.2f}{(w['expectancy_per_trade'] or 0):>8.2f}{(w['return_pct'] or 0):>10.1f}{w['sharpe_annualized']:>8}{(w['dsr'] or 0):>6.2f}"
          f"{(w.get('spread_slippage_and_fees_paid') or 0):>10.0f}{(w.get('financing_paid') or 0):>10.0f}{w.get('trades_held_overnight', 0):>10}  {w.get('exit_reasons')}")
if rows:
    r0, w0, b0, md0 = rows[0][2], rows[0][3], rows[0][4], rows[0][5]
    print(f"\nbuy&hold Sharpe {b0['buy_and_hold_sharpe_annualized']}, return {b0['buy_and_hold_return_pct']}% over the same OOS bars; bars/year used {r0['bars_per_year_used']}")
    print(f"power (1 market, {w0['oos_years']} OOS years, {r0['n_trials_for_deflation']} trials): the true annualised Sharpe must be about {md0['per_market_sharpe_needed']:.2f} "
          f"to be detected with 80 % power (hurdle from luck alone: {md0['hurdle_best_of_n_annual']:.2f})")
