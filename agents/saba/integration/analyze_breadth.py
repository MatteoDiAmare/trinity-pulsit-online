#!/usr/bin/env python3
"""Summarises the breadth experiments: what the same studies say under each data design, and the minimum detectable edge of each design."""
import json, math, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_data.academy.power import min_detectable_sharpe, sharpe_se

d = Path(sys.argv[1])
neff = json.load(open(sys.argv[2]))
N_EFF = {"A_3mkts_10y_newmethod": neff["three10y"]["effective_independent_markets"], "B_3mkts_full": neff["three"]["effective_independent_markets"],
         "C_16mkts_full": neff["sixteen"]["effective_independent_markets"]}
rows, out = [], {}
print(f"{'design':24s} {'family':9s} {'mkts':>4s} {'oosY':>5s} {'trades':>6s} {'hit':>5s} {'exp/trd':>8s} {'CI width':>8s} {'WFO SR':>7s} {'B&H SR':>7s} {'DSR':>5s} {'PBO':>5s} {'cells+':>6s} {'verdict':>11s}")
for f in sorted(d.glob("*.json")):
    r = json.load(open(f))
    design, fam = f.stem.split("__")
    w, b = r["wfo"], r["benchmarks"]
    ci = w["expectancy_ci95"]
    width = (ci[1] - ci[0]) if ci else float("nan")
    print(f"{design:24s} {fam:9s} {len(r['markets']):4d} {w['oos_years']:5.1f} {w['trades']:6d} {w['hit_rate'] or 0:5.2f} {w['expectancy_per_trade']:8.2f} {width:8.2f} {w['sharpe_annualized']:7.2f} {b['buy_and_hold_sharpe_annualized']:7.2f} {w['dsr']:5.2f} {r['pbo']['pbo'] if r['pbo'] else float('nan'):5.2f} {b['grid_share_positive_oos']:6.2f} {r['verdict']:>11s}")
    out.setdefault(design, {})[fam] = {"markets": len(r["markets"]), "oos_years": w["oos_years"], "trades": w["trades"], "n_trials": r["n_trials_for_deflation"],
                                       "hurdle_annual": b["expected_best_of_n_noise_sharpe_annualized"], "bpy": r["bars_per_year_used"], "ci_width": width, "wfo_sharpe": w["sharpe_annualized"], "dsr": w["dsr"]}
print("\nMinimum detectable TRUE per-market Sharpe (alpha 5 %, power 80 %), using each design's years, effective independent markets and best-of-N hurdle (SMA family):")
print(f"{'design':24s} {'years':>6s} {'N_eff':>6s} {'market-years':>12s} {'N trials':>8s} {'hurdle':>7s} {'pooled SR needed':>17s} {'per-market SR needed':>21s}")
power = {}
for design, fams in out.items():
    x = fams["sma"]
    sr0 = x["hurdle_annual"]
    z = 1.6448536269514722 + 0.8416212335729143
    pooled = sr0
    for _ in range(500):
        pooled = sr0 + z * sharpe_se(pooled, x["oos_years"])
    per = pooled / math.sqrt(N_EFF[design])
    power[design] = {"years": x["oos_years"], "n_eff": N_EFF[design], "market_years": x["oos_years"] * N_EFF[design], "n_trials": x["n_trials"], "hurdle": sr0, "pooled_needed": pooled, "per_market_needed": per}
    print(f"{design:24s} {x['oos_years']:6.1f} {N_EFF[design]:6.2f} {x['oos_years'] * N_EFF[design]:12.1f} {x['n_trials']:8d} {sr0:7.2f} {pooled:17.2f} {per:21.2f}")
json.dump({"studies": out, "power": power}, open(d / "_summary.json", "w"), indent=1)
