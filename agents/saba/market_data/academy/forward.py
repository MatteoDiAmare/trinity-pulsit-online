"""Frozen strategies tracked forward on new data, with Saba's own forecast scored afterwards.

Freezing stores the exact plan (strategy, parameters / code hash, markets, interval, capital, code version), each market's data
cutoff (last bar at freezing time), pre-registered pass rules, and Saba's FORECAST (probability that forward expectancy per trade is
positive, expected hit rate, expected expectancy). Nothing about a frozen plan can be edited later.

`update` re-simulates every active candidate from its cutoff on the bars that arrived AFTER it (the strategy sees the earlier history,
never a future bar), so the result is stateless and reproducible: the same bars always give the same snapshot. A position still open at the
last bar is shown separately and excluded from the statistics. Verdicts: insufficient_data until the pre-registered minimum trades and
days have passed; then confirmed / refuted / inconclusive from a bootstrap interval of the per-trade net result.
No LLM, no network (unless `fetch` is asked for) and no orders: this is a shadow account.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .instruments import load_instrument
from .integrity import code_version
from .simulator import SimConfig, Simulator
from .stats import bootstrap_mean_ci, sharpe
from .strategies import build_strategy

DEFAULT_RULES = {"min_trades": 30, "min_days": 60}


def _now():
    return datetime.now(timezone.utc).isoformat()


def validate_forecast(f: dict) -> dict:
    if not isinstance(f, dict):
        raise ValueError("a forecast is required when freezing")
    p, h, e = f.get("p_positive"), f.get("expected_hit_rate"), f.get("expected_expectancy")
    if not isinstance(p, (int, float)) or not 0 < p < 1:
        raise ValueError("forecast.p_positive must be in (0, 1): your probability that the forward expectancy per trade is positive")
    if not isinstance(h, (int, float)) or not 0 <= h <= 1:
        raise ValueError("forecast.expected_hit_rate must be in [0, 1]")
    if not isinstance(e, (int, float)) or not math.isfinite(e):
        raise ValueError("forecast.expected_expectancy must be a number (expected net result per trade in account currency)")
    return {"p_positive": float(p), "expected_hit_rate": float(h), "expected_expectancy": float(e)}


def verdict_from_trades(pnls: list, bars_forward_days: int, rules: dict) -> dict:
    n = len(pnls)
    if n < rules["min_trades"] or bars_forward_days < rules["min_days"]:
        return {"verdict": "insufficient_data", "need": {"trades": rules["min_trades"], "days": rules["min_days"]}, "have": {"trades": n, "days": bars_forward_days}}
    ci = bootstrap_mean_ci(pnls)
    mean = float(np.mean(pnls))
    v = "confirmed" if ci and ci[0] > 0 else "refuted" if ci and ci[1] < 0 else "inconclusive"
    return {"verdict": v, "expectancy": mean, "expectancy_ci95": ci, "hit_rate": float(np.mean([p > 0 for p in pnls])), "trades": n, "days": bars_forward_days}


class ForwardTracker:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS frozen (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, label TEXT NOT NULL, plan_json TEXT NOT NULL,
          plan_sha256 TEXT NOT NULL UNIQUE, cutoffs_json TEXT NOT NULL, forecast_json TEXT NOT NULL, rules_json TEXT NOT NULL,
          study_sha256 TEXT, note TEXT, status TEXT NOT NULL DEFAULT 'active', code_version TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS forward_log (id INTEGER PRIMARY KEY, frozen_id INTEGER NOT NULL, created_at TEXT NOT NULL, as_of TEXT,
          snapshot_json TEXT NOT NULL);""")
        self.db.commit()

    def close(self):
        self.db.close()

    # ---- freezing ------------------------------------------------------
    def freeze(self, archive, *, label: str, strategy: str, params: dict, instruments: list, interval: str = "1d", capital: float = 1000.0,
               forecast: dict, rules: dict | None = None, study_sha256: str | None = None, note: str = "") -> int:
        if not label.strip() or not instruments:
            raise ValueError("label and instruments are required")
        forecast = validate_forecast(forecast)
        rules = {**DEFAULT_RULES, **(rules or {})}
        if rules["min_trades"] < 10 or rules["min_days"] < 20:
            raise ValueError("pre-registered rules need at least 10 trades and 20 days")
        build_strategy(strategy, params)                      # fails now, not later, if the plan is invalid (no process is started)
        cutoffs = {}
        for iid in instruments:
            spec = load_instrument(iid)
            bars = archive.read(spec.symbol, interval)
            cutoffs[iid] = bars.index[-1].isoformat()
        plan = {"strategy": strategy, "params": params, "instruments": list(instruments), "interval": interval, "capital": capital}
        sha = hashlib.sha256(json.dumps({**plan, "cutoffs": cutoffs}, sort_keys=True, default=str).encode()).hexdigest()
        try:
            cur = self.db.execute("INSERT INTO frozen(created_at,label,plan_json,plan_sha256,cutoffs_json,forecast_json,rules_json,study_sha256,note,code_version) "
                                  "VALUES (?,?,?,?,?,?,?,?,?,?)", (_now(), label.strip(), json.dumps(plan, sort_keys=True, default=str), sha, json.dumps(cutoffs),
                                                                   json.dumps(forecast), json.dumps(rules), study_sha256, note, code_version()))
        except sqlite3.IntegrityError:
            raise ValueError("an identical plan is already frozen at the same data cutoff") from None
        self.db.commit()
        return int(cur.lastrowid)

    def get(self, fid: int) -> dict:
        row = self.db.execute("SELECT id,created_at,label,plan_json,plan_sha256,cutoffs_json,forecast_json,rules_json,study_sha256,note,status,code_version "
                              "FROM frozen WHERE id=?", (fid,)).fetchone()
        if row is None:
            raise ValueError("unknown frozen candidate")
        keys = ("id", "created_at", "label", "plan", "plan_sha256", "cutoffs", "forecast", "rules", "study_sha256", "note", "status", "code_version")
        d = dict(zip(keys, row))
        for k in ("plan", "cutoffs", "forecast", "rules"):
            d[k] = json.loads(d[k])
        return d

    def active(self) -> list[dict]:
        return [self.get(r[0]) for r in self.db.execute("SELECT id FROM frozen WHERE status='active' ORDER BY id")]

    def retire(self, fid: int, reason: str):
        self.get(fid)
        self.db.execute("UPDATE frozen SET status='retired', note=note || ? WHERE id=?", (f" | retired: {reason}", fid))
        self.db.commit()

    # ---- forward evaluation ---------------------------------------------
    def snapshot(self, archive, cand: dict) -> dict:
        plan, ids = cand["plan"], cand["plan"]["instruments"]
        per, pnls, days, rets_acc, bh_acc, as_of = {}, [], 0, {}, {}, None
        for iid in ids:
            spec = load_instrument(iid)
            bars = archive.read(spec.symbol, plan["interval"])
            cutoff = pd.Timestamp(cand["cutoffs"][iid])
            start = int((bars.index <= cutoff).sum())
            if start >= len(bars):
                per[iid] = {"bars_forward": 0}
                continue
            cfg = SimConfig(plan["capital"], None, None, instrument=spec)
            res = Simulator(cfg).run(bars, build_strategy(plan["strategy"], plan["params"]), trade_start=start)
            closed = [t for t in res.trades if t["exit_reason"] != "end_of_data"]
            open_pos = [t for t in res.trades if t["exit_reason"] == "end_of_data"]
            eq = res.equity["equity"].to_numpy(dtype=float)
            prev = np.concatenate([[plan["capital"]], eq[:-1]])
            r = eq / prev - 1
            for ts, x in zip(res.equity.index, r):
                rets_acc[str(ts.date())] = rets_acc.get(str(ts.date()), 0.0) + float(x) / len(ids)
            cl = bars["Close"].iloc[start:].pct_change().fillna(0.0)
            for ts, x in zip(cl.index, cl):
                bh_acc[str(ts.date())] = bh_acc.get(str(ts.date()), 0.0) + float(x) / len(ids)
            pn = [float(t["net_pnl"]) for t in closed]
            pnls.extend(pn)
            days = max(days, len(bars) - start)
            as_of = max(as_of or bars.index[-1], bars.index[-1])
            per[iid] = {"bars_forward": len(bars) - start, "closed_trades": len(pn), "open_position": bool(open_pos),
                        "net_return_pct": float(eq[-1] / plan["capital"] * 100 - 100), "last_bar": str(bars.index[-1])}
        dates = sorted(rets_acc)
        pooled = np.array([rets_acc[d] for d in dates])
        bh = np.array([bh_acc.get(d, 0.0) for d in dates])
        bpy = float(np.mean([load_instrument(i).bars_per_year(plan["interval"]) for i in ids]))
        v = verdict_from_trades(pnls, days, cand["rules"])
        return {"as_of": str(as_of) if as_of is not None else None, "bars_forward": days, "closed_trades": len(pnls), "per_market": per,
                "hit_rate": float(np.mean([p > 0 for p in pnls])) if pnls else None, "expectancy": float(np.mean(pnls)) if pnls else None,
                "avg_win": float(np.mean([p for p in pnls if p > 0])) if any(p > 0 for p in pnls) else None,
                "avg_loss": float(np.mean([p for p in pnls if p < 0])) if any(p < 0 for p in pnls) else None,
                "pnls": pnls, "sharpe_annualized": float(sharpe(pooled) * math.sqrt(bpy)) if len(pooled) > 2 else None,
                "return_pct": float(np.prod(1 + pooled) - 1) * 100 if len(pooled) else None,
                "buy_and_hold_return_pct": float(np.prod(1 + bh) - 1) * 100 if len(bh) else None, "verdict": v, "code_version": code_version()}

    def update(self, archive, *, fetch: bool = False) -> list[dict]:
        cands = self.active()
        fetch_errors = {}
        if fetch:
            for sym, itv in sorted({(load_instrument(i).symbol, c["plan"]["interval"]) for c in cands for i in c["plan"]["instruments"]}):
                try:
                    archive.ingest(sym, itv, period="1mo")
                except Exception as exc:
                    fetch_errors[sym] = str(exc)[:160]
        out = []
        for c in cands:
            snap = self.snapshot(archive, c)
            if fetch_errors:
                snap["fetch_errors"] = fetch_errors
            self.db.execute("INSERT INTO forward_log(frozen_id,created_at,as_of,snapshot_json) VALUES (?,?,?,?)", (c["id"], _now(), snap["as_of"], json.dumps(snap, default=str)))
            out.append({"id": c["id"], "label": c["label"], **snap})
        self.db.commit()
        return out

    def history(self, fid: int) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT snapshot_json FROM forward_log WHERE frozen_id=? ORDER BY id", (fid,))]

    # ---- scoring Saba's forecasts ----------------------------------------
    def forecast_report(self) -> dict:
        rows, briers = [], []
        for r in self.db.execute("SELECT id FROM frozen ORDER BY id"):
            c = self.get(r[0])
            hist = self.history(c["id"])
            last = hist[-1] if hist else None
            row = {"id": c["id"], "label": c["label"], "status": c["status"], "forecast": c["forecast"],
                   "verdict": (last or {}).get("verdict", {}).get("verdict", "not_evaluated")}
            if last and row["verdict"] in ("confirmed", "refuted", "inconclusive") and last.get("expectancy") is not None:
                y = 1.0 if last["expectancy"] > 0 else 0.0
                p = c["forecast"]["p_positive"]
                row.update({"outcome_positive": bool(y), "brier": (p - y) ** 2, "hit_rate_error": (last["hit_rate"] - c["forecast"]["expected_hit_rate"]) if last["hit_rate"] is not None else None,
                            "expectancy_error": last["expectancy"] - c["forecast"]["expected_expectancy"], "realized_expectancy": last["expectancy"], "realized_hit_rate": last["hit_rate"]})
                briers.append(row["brier"])
            rows.append(row)
        scored = len(briers)
        return {"candidates": rows, "scored": scored, "mean_brier": float(np.mean(briers)) if briers else None,
                "baseline_brier_always_0.5": 0.25, "skill_vs_coin": (1 - float(np.mean(briers)) / 0.25) if briers else None,
                "note": "Brier 0 is perfect, 0.25 equals always answering 50 %. Skill > 0 means her stated probabilities beat a coin flip; it needs many scored candidates to mean anything."}
