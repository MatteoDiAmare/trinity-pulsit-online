"""Session report: what was actually run, what it earned per trade, how it compares with benchmarks, what was learned.

Everything is computed from the stored evidence (registry, ledgers, memory, archive) of a data root; nothing is taken from
the agent's own summaries. `baseline` marks what existed before the session (max ids), so session numbers are a delta.
Benchmarks for every (instrument, period): buy-and-hold, the luck benchmark recorded with the experiment, and a seeded
random-control distribution run with the same instrument spec, costs and bars.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from ..archive import MarketArchive
from .evaluation import compute_metrics
from .ledger import LedgerStore
from .simulator import SimConfig, Simulator
from .strategies import build_strategy

PERIODS = ("train", "validation", "final_test")


# ---------------------------------------------------------------- loading
def _rows(db, sql, args=()):
    cur = db.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def load(root, baseline: dict, owner="local", org="personal", upper: int | None = None) -> list[dict]:
    """One record per training experiment created after the baseline, with all its period ledgers."""
    root = Path(root)
    reg = sqlite3.connect(f"file:{root / 'experiments.sqlite3'}?mode=ro", uri=True)
    store = LedgerStore(root, owner, org)
    out = []
    for row in _rows(reg, "SELECT id,name,configuration_json,results_json FROM experiments WHERE id>? AND id<=? ORDER BY id",
                     (baseline["max_registry_id"], upper if upper is not None else 10**12)):
        cfg, res = json.loads(row["configuration_json"]), json.loads(row["results_json"])
        if not row["name"].startswith("academy:") or "params" not in cfg or row["name"] == "academy:final_test":
            continue
        eid = row["id"]
        try:
            periods = {p: store.read_period(eid, p) for p in store.periods(eid)}
        except ValueError:
            continue
        label = cfg.get("label") or cfg["strategy"]
        out.append({"id": eid, "strategy": cfg["strategy"], "label": label, "symbol": cfg["symbol"], "interval": cfg["interval"],
                    "instrument": cfg.get("instrument_id"), "config": cfg, "results": res, "periods": periods,
                    "trial_number": res.get("trial_number_on_market")})
    reg.close()
    return out


# ---------------------------------------------------------------- trade statistics
def trade_stats(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "wins": 0, "losses": 0, "hit_rate": None, "avg_win": None, "avg_loss": None, "expectancy": None,
                "expectancy_pct": None, "total_net": 0.0, "avg_cost": None, "avg_r": None, "payoff_ratio": None}
    pnl = np.array([t["net_pnl"] for t in trades], dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    r = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    return {"trades": n, "wins": int(len(wins)), "losses": int(len(losses)), "hit_rate": float(len(wins) / n),
            "avg_win": float(wins.mean()) if len(wins) else None, "avg_loss": float(losses.mean()) if len(losses) else None,
            "expectancy": float(pnl.mean()), "expectancy_pct": float(np.mean([t["net_return_pct"] for t in trades])),
            "total_net": float(pnl.sum()), "avg_cost": float(np.mean([t["fees"] + t["spread_slippage_cost"] for t in trades])),
            "avg_r": float(np.mean(r)) if r else None,
            "payoff_ratio": (float(wins.mean() / -losses.mean()) if len(wins) and len(losses) else None)}


def bootstrap_ci(values, n=4000, seed=0):
    v = np.asarray(values, dtype=float)
    if len(v) < 5:
        return None
    rng = np.random.default_rng(seed)
    means = v[rng.integers(0, len(v), size=(n, len(v)))].mean(axis=1)
    return [float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))]


# ---------------------------------------------------------------- benchmarks
def _period_bounds(payload):
    eq = payload["equity"]
    return pd.Timestamp(eq[0]["timestamp"]), pd.Timestamp(eq[-1]["timestamp"])


def buy_and_hold(bars: pd.DataFrame, start, end, bars_per_year: float) -> dict:
    seg = bars.loc[(bars.index >= start) & (bars.index <= end), "Close"].to_numpy(dtype=float)
    if len(seg) < 3:
        return {"return_pct": None, "sharpe": None}
    r = seg[1:] / seg[:-1] - 1
    sd = r.std(ddof=1)
    return {"return_pct": float((seg[-1] / seg[0] - 1) * 100), "sharpe": float(r.mean() / sd * math.sqrt(bars_per_year)) if sd > 0 else 0.0}


def random_null(bars: pd.DataFrame, cfg: SimConfig, start, end, interval: str, runs: int, seed: int = 0) -> dict:
    s_idx, e_idx = bars.index.get_loc(start), bars.index.get_loc(end)
    rows = []
    for k in range(runs):
        strat = build_strategy("random_control", {"seed": seed * 7919 + k, "trade_prob": 0.02, "hold_bars": 10})
        res = Simulator(cfg).run(bars.iloc[:e_idx + 1], strat, trade_start=s_idx)
        m = compute_metrics(res, interval)
        st = trade_stats(res.trades)
        rows.append((m["sharpe_annualized"] or 0.0, m["net_return_pct"] or 0.0, st["hit_rate"], st["expectancy"], st["trades"]))
    a = np.array([[x if x is not None else np.nan for x in r] for r in rows], dtype=float)
    q = lambda col, p: float(np.nanpercentile(a[:, col], p))
    return {"runs": runs, "sharpe_mean": float(np.nanmean(a[:, 0])), "sharpe_p95": q(0, 95), "return_mean_pct": float(np.nanmean(a[:, 1])),
            "return_p95_pct": q(1, 95), "hit_rate_mean": float(np.nanmean(a[:, 2])), "expectancy_mean": float(np.nanmean(a[:, 3])),
            "trades_mean": float(np.nanmean(a[:, 4]))}


# ---------------------------------------------------------------- report
def build_report(root, baseline: dict, *, random_runs: int = 100, owner="local", org="personal", transcript_text: str | None = None) -> dict:
    root = Path(root)
    exps = load(root, baseline, owner, org)
    archive = MarketArchive(root)
    bars_cache, null_cache = {}, {}

    def bars_for(sym, interval):
        if (sym, interval) not in bars_cache:
            bars_cache[(sym, interval)] = archive.read(sym, interval)
        return bars_cache[(sym, interval)]

    # ---- per experiment/period rows
    rows = []
    for e in exps:
        cfg = SimConfig.from_dict(e["config"]["sim_config"])
        bpy = cfg.instrument.bars_per_year(e["interval"]) if cfg.instrument else 252.0
        for p, payload in e["periods"].items():
            t = payload["trades"]
            st = trade_stats(t)
            m = payload["risk"]["metrics"]
            start, end = _period_bounds(payload)
            bars = bars_for(e["symbol"], e["interval"])
            bh = buy_and_hold(bars, start, end, bpy)
            key = (e["symbol"], str(start), str(end), json.dumps(e["config"]["sim_config"], sort_keys=True))
            if random_runs and key not in null_cache:
                null_cache[key] = random_null(bars, cfg, start, end, e["interval"], random_runs)
            nb = e["results"].get("trial_number_on_market")
            from .integrity import expected_max_noise_sharpe
            luck = expected_max_noise_sharpe(nb, m["bars"], bpy) if (nb and p == "validation") else None
            rows.append({"id": e["id"], "label": e["label"], "strategy": e["strategy"], "symbol": e["symbol"], "instrument": e["instrument"],
                         "period": p, "start": str(start)[:10], "end": str(end)[:10], **st, "net_return_pct": m["net_return_pct"],
                         "sharpe": m["sharpe_annualized"], "max_dd_pct": m["max_drawdown_pct"], "bh_return_pct": bh["return_pct"],
                         "bh_sharpe": bh["sharpe"], "luck_benchmark_sharpe": luck, "null": null_cache.get(key), "trial_number": nb,
                         "_trades": t})

    def pool(sel):
        trades = [t for r in sel for t in r["_trades"]]
        s = trade_stats(trades)
        s["expectancy_ci95"] = bootstrap_ci([t["net_pnl"] for t in trades])
        return s

    by_period = {p: pool([r for r in rows if r["period"] == p]) for p in PERIODS}
    overall = pool(rows)
    strategies = {}
    for lab, mkt in sorted({(r["label"], r["instrument"] or r["symbol"]) for r in rows}):
        sel = lambda p: [r for r in rows if r["label"] == lab and (r["instrument"] or r["symbol"]) == mkt and r["period"] == p]
        strategies[f"{lab} @ {mkt}"] = {p: pool(sel(p)) for p in PERIODS if sel(p)}
    # ---- time trend inside the session (validation + final test are the only periods that are not fitted on)
    ordered = sorted({r["id"] for r in rows})
    half = len(ordered) // 2
    first_ids, second_ids = set(ordered[:half]), set(ordered[half:])
    val = [r for r in rows if r["period"] == "validation"]
    trend = {}
    for name, ids in (("first_half", first_ids), ("second_half", second_ids)):
        sel = [r for r in val if r["id"] in ids]
        trades = [t["net_pnl"] for r in sel for t in r["_trades"]]
        ex = [r["sharpe"] - r["luck_benchmark_sharpe"] for r in sel if r["sharpe"] is not None and r["luck_benchmark_sharpe"] is not None]
        trend[name] = {"experiments": len(sel), "trades": len(trades), "hit_rate": float(np.mean([x > 0 for x in trades])) if trades else None,
                       "expectancy": float(np.mean(trades)) if trades else None,
                       "mean_sharpe": float(np.mean([r["sharpe"] for r in sel if r["sharpe"] is not None])) if sel else None,
                       "mean_excess_over_luck": float(np.mean(ex)) if ex else None,
                       "share_above_luck": float(np.mean([e_ > 0 for e_ in ex])) if ex else None}
    finals = [{"id": r["id"], "label": r["label"], "symbol": r["symbol"], "trades": r["trades"], "hit_rate": r["hit_rate"],
               "net_return_pct": r["net_return_pct"], "sharpe": r["sharpe"], "bh_sharpe": r["bh_sharpe"]} for r in rows if r["period"] == "final_test"]
    # ---- memory
    mem = sqlite3.connect(f"file:{root / 'academy_memory.sqlite3'}?mode=ro", uri=True)
    lessons = _rows(mem, "SELECT id,hypothesis_id,verdict,confidence,claim,evidence_json,created_at FROM lessons WHERE id>? ORDER BY id", (baseline["max_lesson_id"],))
    hyps = _rows(mem, "SELECT id,status,family,trial_budget,trials_used,statement,created_at FROM hypotheses WHERE id>? ORDER BY id", (baseline["max_hyp_id"],))
    cycles = _rows(mem, "SELECT * FROM cycles WHERE id>? ORDER BY id", (baseline["max_cycle_id"],))
    cites = []
    all_prior_ids = {x["id"] for x in _rows(mem, "SELECT id FROM lessons WHERE id<=?", (baseline["max_lesson_id"],))}
    for h in hyps:
        refs = sorted({int(m) for m in re.findall(r"\bL(\d+)\b", h["statement"])})
        if refs:
            cites.append({"hypothesis": h["id"], "cites_lessons": refs, "text": h["statement"][:200]})
    for l in lessons:
        refs = sorted({int(m) for m in re.findall(r"\bL(\d+)\b", l["claim"])} - {l["id"]})
        if refs:
            cites.append({"lesson": l["id"], "cites_lessons": refs, "text": l["claim"][:200]})
    for l in lessons:
        l["evidence"] = [e["experiment_id"] for e in json.loads(l.pop("evidence_json"))]
    mem.close()
    # ---- session vs what existed before it (validation periods only: neither is fitted on them by the strategy itself)
    pre_exps = load(root, {"max_registry_id": 0}, owner, org, upper=baseline["max_registry_id"]) if baseline["max_registry_id"] else []
    pre_trades = [t for e in pre_exps for t in e["periods"].get("validation", {}).get("trades", [])]
    ses_trades = [t for r in val for t in r["_trades"]]
    comparison = {"pre_session_validation": trade_stats(pre_trades), "session_validation": trade_stats(ses_trades)}
    if pre_trades and ses_trades:
        a, b = np.array([t["net_pnl"] > 0 for t in pre_trades]), np.array([t["net_pnl"] > 0 for t in ses_trades])
        pp = (a.sum() + b.sum()) / (len(a) + len(b))
        se = math.sqrt(pp * (1 - pp) * (1 / len(a) + 1 / len(b))) or float("nan")
        z = (b.mean() - a.mean()) / se
        rng = np.random.default_rng(1)
        pa, pb = np.array([t["net_pnl"] for t in pre_trades]), np.array([t["net_pnl"] for t in ses_trades])
        diffs = pb[rng.integers(0, len(pb), (4000, len(pb)))].mean(1) - pa[rng.integers(0, len(pa), (4000, len(pa)))].mean(1)
        comparison.update({"hit_rate_diff": float(b.mean() - a.mean()), "hit_rate_z": float(z),
                           "hit_rate_p_two_sided": float(math.erfc(abs(z) / math.sqrt(2))) if math.isfinite(z) else None,
                           "expectancy_diff": float(pb.mean() - pa.mean()), "expectancy_diff_ci95": [float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))]})
    excerpts = []
    if transcript_text:
        for line in transcript_text.splitlines():
            if re.search(r"\bL\d+\b|dead end|prior|earlier|benchmark|final test|refuted|pattern", line, re.I) and len(line.strip()) > 30:
                excerpts.append(line.strip()[:420])
    clean_rows = [{k: v for k, v in r.items() if k != "_trades"} for r in rows]
    return {"baseline": baseline, "experiments": len(exps), "experiments_by_label": {k: sum(1 for e in exps if e["label"] == k) for k in sorted({e["label"] for e in exps})},
            "experiments_by_symbol": {k: sum(1 for e in exps if e["symbol"] == k) for k in sorted({e["symbol"] for e in exps})},
            "rows": clean_rows, "by_period": by_period, "overall": overall, "by_strategy": strategies, "trend": trend, "final_tests": finals,
            "lessons": lessons, "hypotheses": hyps, "cycles": cycles, "citations": cites, "random_runs": random_runs,
            "final_test_experiments": sum(1 for r in rows if r["period"] == "final_test"), "comparison_to_pre_session": comparison,
            "narration_excerpts": excerpts[:30]}


# ---------------------------------------------------------------- rendering
def _f(x, nd=2, pct=False):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "-"
    return f"{x * 100:.{nd}f}%" if pct else f"{x:.{nd}f}"


def render_markdown(rep: dict) -> str:
    L = []
    P = L.append
    P(f"# Saba session report\n\nExperiments created in the session: **{rep['experiments']}** training experiments (by market {rep['experiments_by_symbol']}) plus **{rep['final_test_experiments']}** final tests; "
      f"cycles: {len(rep['cycles'])}; new hypotheses: {len(rep['hypotheses'])}; new lessons: {len(rep['lessons'])}.\n")
    P("## 1. Experiments and backtest trades\n\n| Period | trades | wins | losses |\n|---|---|---|---|")
    for p, s in rep["by_period"].items():
        P(f"| {p} | {s['trades']} | {s['wins']} | {s['losses']} |")
    P(f"| **all** | {rep['overall']['trades']} | {rep['overall']['wins']} | {rep['overall']['losses']} |\n")
    P("## 2. Hit rate per strategy and in total (trades in brackets)\n\n| Strategy | train | validation | final test |\n|---|---|---|---|")
    for lab, d in rep["by_strategy"].items():
        cell = lambda p: f"{_f(d[p]['hit_rate'], 0, True)} ({d[p]['trades']})" if p in d else "-"
        P(f"| {lab} | {cell('train')} | {cell('validation')} | {cell('final_test')} |")
    P("| **total** | " + " | ".join(f"{_f(s['hit_rate'], 0, True)} ({s['trades']})" for s in rep["by_period"].values()) + " |\n")
    P("## 3. Average win / loss and expected net result per trade (net of fees, spread, slippage; account 1000)\n\n"
      "| Scope | trades | avg win | avg loss | payoff | expectancy/trade | 95% CI | avg cost/trade |\n|---|---|---|---|---|---|---|---|")
    scopes = [(p, s) for p, s in rep["by_period"].items()] + [("all", rep["overall"])]
    for name, s in scopes:
        P(f"| {name} | {s['trades']} | {_f(s['avg_win'])} | {_f(s['avg_loss'])} | {_f(s['payoff_ratio'])} | {_f(s['expectancy'])} | "
          f"{('[%s, %s]' % (_f(s['expectancy_ci95'][0]), _f(s['expectancy_ci95'][1]))) if s.get('expectancy_ci95') else '-'} | {_f(s['avg_cost'])} |")
    P("\n## 4. Risk-adjusted return vs benchmarks (annualized Sharpe; validation and final-test rows)\n")
    P("| exp | strategy | market | period | trades | return% | Sharpe | buy&hold Sharpe | luck bench | random null mean / p95 |\n|---|---|---|---|---|---|---|---|---|---|")
    for r in rep["rows"]:
        if r["period"] == "train":
            continue
        n = r["null"]
        P(f"| {r['id']} | {r['label']} | {r['instrument'] or r['symbol']} | {r['period']} | {r['trades']} | {_f(r['net_return_pct'])} | {_f(r['sharpe'])} | "
          f"{_f(r['bh_sharpe'])} | {_f(r['luck_benchmark_sharpe'])} | {(_f(n['sharpe_mean']) + ' / ' + _f(n['sharpe_p95'])) if n else '-'} |")
    P("\n## 5. Lessons and their effect on later work\n")
    for l in rep["lessons"]:
        P(f"- **L{l['id']}** [{l['verdict']}, conf {l['confidence']:.2f}] (H{l['hypothesis_id']}, evidence exp {l['evidence']}): {l['claim']}")
    P("\nExplicit citations of earlier lessons in later hypotheses/lessons:")
    for c in rep["citations"]:
        who = f"H{c['hypothesis']}" if "hypothesis" in c else f"L{c['lesson']}"
        P(f"- {who} cites L{c['cites_lessons']}: {c['text']}")
    if rep["narration_excerpts"]:
        P("\nHer own narration referring to earlier findings (excerpts):")
        for x in rep["narration_excerpts"]:
            P(f"- {x}")
    P("\n## 6. Evidence of improvement during the session\n")
    t = rep["trend"]
    P("| Validation, first vs second half of the session | experiments | trades | hit rate | expectancy/trade | mean Sharpe | mean excess over luck | share above luck |\n|---|---|---|---|---|---|---|---|")
    for k in ("first_half", "second_half"):
        d = t[k]
        P(f"| {k} | {d['experiments']} | {d['trades']} | {_f(d['hit_rate'], 0, True)} | {_f(d['expectancy'])} | {_f(d['mean_sharpe'])} | {_f(d['mean_excess_over_luck'])} | {_f(d['share_above_luck'], 0, True)} |")
    c = rep["comparison_to_pre_session"]
    if "hit_rate_p_two_sided" in c:
        pre, ses = c["pre_session_validation"], c["session_validation"]
        P(f"\nValidation trades before the session vs during it: hit rate {_f(pre['hit_rate'], 0, True)} ({pre['trades']}) -> {_f(ses['hit_rate'], 0, True)} ({ses['trades']}), "
          f"difference {_f(c['hit_rate_diff'], 1, True)}, two-sided p = {_f(c['hit_rate_p_two_sided'], 2)}; expectancy/trade {_f(pre['expectancy'])} -> {_f(ses['expectancy'])}, "
          f"difference {_f(c['expectancy_diff'])}, 95% CI [{_f(c['expectancy_diff_ci95'][0])}, {_f(c['expectancy_diff_ci95'][1])}].")
    P("\nFinal tests (the only unfitted evidence): " + ("; ".join(f"exp {f['id']} {f['label']} {f['symbol']}: {f['trades']} trades, hit {_f(f['hit_rate'], 0, True)}, return {_f(f['net_return_pct'])}%, Sharpe {_f(f['sharpe'])} (buy&hold {_f(f['bh_sharpe'])})" for f in rep["final_tests"]) or "none"))
    return "\n".join(L) + "\n"
