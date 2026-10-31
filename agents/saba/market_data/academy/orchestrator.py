"""Autonomous research cycle: budgets, kill-switch, hypothesis runs, analysis and next-step proposals.

Saba needs no per-experiment approval. The limits that keep the loop safe and honest are code, not prompts:
  * Budget (configurable file): max experiments per cycle (default 20), max cycles per rolling 24 h (default 4),
    max wall-clock seconds per cycle.
  * Kill-switch: a halt file stops every new cycle/run. Saba may halt herself; only the operator resumes
    (CLI), because an agent must not be able to lift its own stop.
  * Pre-registration: a hypothesis's plan is fixed and hashed at registration; running executes exactly that plan,
    and spends its own trial budget. New ideas = new (child) hypotheses.
  * Every run goes through the normal recording path, so dedupe, trial counting, causality gate and the
    final-test protection all still apply.
Nothing here touches a broker; there is no live execution.
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from core.exploration_policy import Opportunity, rank
from .compare import compare_markets
from .instruments import load_instrument
from .integrity import SpecIndex, market_key, test_frontier
from .memory import ResearchMemory, _sha
from .simulator import SimConfig
from .spec_strategy import SpecError, TIME_FEATURES, validate_spec
from .strategies import build_strategy


class CycleDenied(Exception):
    pass


@dataclass(frozen=True)
class Budget:
    max_experiments_per_cycle: int = 20
    max_cycles_per_day: int = 4
    max_seconds_per_cycle: int = 1800

    def __post_init__(self):
        if not 1 <= self.max_experiments_per_cycle <= 1000 or not 1 <= self.max_cycles_per_day <= 96 \
                or not 30 <= self.max_seconds_per_cycle <= 86_400:
            raise ValueError("budget out of range")

    @staticmethod
    def load(path) -> "Budget":
        p = Path(path)
        return Budget(**json.loads(p.read_text())) if p.is_file() else Budget()

    def save(self, path):
        Path(path).write_text(json.dumps(asdict(self), indent=1) + "\n")


class Orchestrator:
    def __init__(self, root, registry, archive, memory: ResearchMemory, ledger=None, budget: Budget | None = None, clock=None):
        self.root = Path(root).expanduser()
        self.registry, self.archive, self.memory, self.ledger = registry, archive, memory, ledger
        self.budget = budget or Budget.load(self.root / "academy_budget.json")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.halt_path = self.root / "academy_halt.json"

    # ---- kill-switch -------------------------------------------------------
    def halted(self) -> dict | None:
        if self.halt_path.is_file():
            try:
                return json.loads(self.halt_path.read_text())
            except json.JSONDecodeError:
                return {"halted": True, "reason": "unreadable halt file (treated as halted)"}
        return None

    def halt(self, reason: str, by: str = "saba"):
        if not reason.strip():
            raise ValueError("a reason is required")
        self.root.mkdir(parents=True, exist_ok=True)
        self.halt_path.write_text(json.dumps({"halted": True, "reason": reason.strip(), "by": by,
                                              "at": self.clock().isoformat()}) + "\n")

    def resume(self):
        """Operator action (CLI only); deliberately not exposed through the agent bridge."""
        self.halt_path.unlink(missing_ok=True)

    # ---- cycles ----------------------------------------------------------
    def start_cycle(self) -> dict:
        h = self.halted()
        if h:
            raise CycleDenied(f"halted: {h.get('reason')}")
        now = self.clock()
        since = (now - timedelta(hours=24)).isoformat()
        n = self.memory.db.execute("SELECT COUNT(*) FROM cycles WHERE started_at > ?", (since,)).fetchone()[0]
        if n >= self.budget.max_cycles_per_day:
            raise CycleDenied(f"cycle budget used: {n}/{self.budget.max_cycles_per_day} in the last 24 h")
        cur = self.memory.db.execute("INSERT INTO cycles(started_at,status) VALUES (?, 'running')", (now.isoformat(),))
        self.memory.db.commit()
        return {"cycle_id": int(cur.lastrowid), "started_at": now.isoformat(),
                "experiment_budget": self.budget.max_experiments_per_cycle, "cycles_used_24h": n + 1}

    def _cycle(self, cycle_id: int) -> dict:
        row = self.memory.db.execute("SELECT * FROM cycles WHERE id=?", (cycle_id,)).fetchone()
        if row is None:
            raise ValueError("unknown cycle")
        return dict(row)

    def finish_cycle(self, cycle_id: int, status: str = "done") -> dict:
        c = self._cycle(cycle_id)
        self.memory.db.execute("UPDATE cycles SET finished_at=?, status=? WHERE id=?", (self.clock().isoformat(), status, cycle_id))
        self.memory.db.commit()
        return {**c, "status": status}

    def status(self) -> dict:
        now = self.clock()
        n = self.memory.db.execute("SELECT COUNT(*) FROM cycles WHERE started_at > ?", ((now - timedelta(hours=24)).isoformat(),)).fetchone()[0]
        return {"halted": self.halted(), "budget": asdict(self.budget), "cycles_last_24h": n,
                "open_hypotheses": len(self.memory.open_hypotheses())}

    # ---- running a pre-registered hypothesis -----------------------------
    def run_hypothesis(self, cycle_id: int, hypothesis_id: int, *, capital: float = 1000.0) -> dict:
        h = self.halted()
        if h:
            raise CycleDenied(f"halted: {h.get('reason')}")
        cyc = self._cycle(cycle_id)
        if cyc["status"] != "running":
            raise CycleDenied("cycle is not running")
        started = datetime.fromisoformat(cyc["started_at"])
        if (self.clock() - started).total_seconds() > self.budget.max_seconds_per_cycle:
            raise CycleDenied("cycle time budget exhausted")
        hyp = self.memory.hypothesis(hypothesis_id)
        if hyp is None:
            raise ValueError("unknown hypothesis")
        if hyp["status"] not in ("open", "testing"):
            raise ValueError(f"hypothesis is {hyp['status']}; register a new (child) hypothesis instead")
        if not hyp["plan_intact"]:
            raise ValueError("hypothesis plan differs from its pre-registration")
        plan = hyp["plan"]
        cells = len(plan["experiments"]) * len(plan["instruments"])
        left = self.budget.max_experiments_per_cycle - cyc["experiments_run"]
        if cells > left:
            raise CycleDenied(f"plan needs up to {cells} experiments but the cycle has {left} left")
        if cells > hyp["trials_remaining"]:
            raise ValueError(f"trial budget of hypothesis {hypothesis_id} too small: {cells} planned, {hyp['trials_remaining']} left")
        specs = [load_instrument(i) for i in plan["instruments"]]
        interval = plan.get("interval", "1d")
        index = SpecIndex(self.registry)
        before = sum(index.trials(market_key(s.symbol, interval)) for s in specs)
        report = None
        try:
            report = compare_markets(self.registry, self.archive, plan["experiments"], specs, interval=interval,
                                     capital=capital, ledger=self.ledger)
        finally:
            new = sum(index.trials(market_key(s.symbol, interval)) for s in specs) - before
            if new:
                self.memory.spend_trials(hypothesis_id, min(new, hyp["trials_remaining"]))
                self.memory.db.execute("UPDATE cycles SET experiments_run = experiments_run + ? WHERE id=?", (new, cycle_id))
                self.memory.db.commit()
        for info in report["market_data"].values():
            self.memory.link_experiments(hypothesis_id, info["experiment_ids"])
        self.memory.set_status(hypothesis_id, "testing")
        return {"hypothesis_id": hypothesis_id, "new_experiments": new, "report": report, "analysis": self.analyze(report)}

    @staticmethod
    def analyze(report: dict) -> list[dict]:
        """Rule-based reading of a comparison; a suggestion list for Saba, not a verdict."""
        out = []
        for label, m in report["matrix"].items():
            ok = {iid: c for iid, c in m["per_market"].items() if c["status"] == "ok"}
            if not ok:
                out.append({"strategy": label, "suggestion": "drop", "reason": "rejected by the causality gate", "experiment_ids": []})
                continue
            ids = [c["experiment_id"] for c in ok.values()]
            good = {i: c for i, c in ok.items() if c["exceeds_noise"] and (c["validation"]["net_return_pct"] or 0) > 0}
            positive = [i for i, c in ok.items() if (c["validation"]["net_return_pct"] or 0) > 0 and c["validation"]["trades"] >= 5]
            if good:
                best = max(good, key=lambda i: good[i]["validation"]["sharpe_annualized"] or -9)
                out.append({"strategy": label, "suggestion": "confirm", "markets": sorted(good), "best_market": best,
                            "reason": "validation Sharpe above the luck benchmark with positive return; confirm ONE candidate on its own fresh final-test window",
                            "experiment_ids": ids})
            elif positive:
                out.append({"strategy": label, "suggestion": "vary", "markets": sorted(positive),
                            "reason": "positive validation but not above the luck benchmark; test variants as a new hypothesis (more trials raise the bar)",
                            "experiment_ids": ids})
            else:
                out.append({"strategy": label, "suggestion": "drop", "reason": "no market with positive validation and enough trades; record a refuted lesson",
                            "experiment_ids": ids})
        return out

    # ---- what next -------------------------------------------------------------
    def propose_next(self) -> list[dict]:
        """Rank open hypotheses with the existing core.exploration_policy (value + curiosity - cost)."""
        opps, by_name = [], {}
        for h in self.memory.open_hypotheses():
            cells = len(h["plan"]["experiments"]) * len(h["plan"]["instruments"])
            candidates = self.memory.search(h["family"], verdict="candidate", limit=5)
            name = f"H{h['id']}"
            opps.append(Opportunity(name, 0.2 + 0.3 * bool(candidates), h["trials_remaining"] / h["trial_budget"],
                                    cells / self.budget.max_experiments_per_cycle))
            by_name[name] = (h, cells)
        return [{"hypothesis_id": by_name[o.name][0]["id"], "statement": by_name[o.name][0]["statement"],
                 "cells": by_name[o.name][1], "score": o.expected_value + 0.2 * o.uncertainty - o.cost,
                 "fits_remaining_budget": by_name[o.name][1] <= by_name[o.name][0]["trials_remaining"]}
                for o in rank(opps)]

    def test_windows(self, interval: str = "1d") -> dict:
        out = {}
        for sid in ("GOLD", "EURUSD", "SP500"):
            spec = load_instrument(sid)
            f = test_frontier(self.registry, spec.symbol, interval)
            out[sid] = None if f is None else f.isoformat()
        return out


def mutate_spec(spec: dict, n: int, seed: int) -> list[dict]:
    """Deterministic neighbours of a JSON spec: jittered windows/thresholds and stop/take-profit variants.
    Each variant is validated; duplicates and the original are skipped. Variants are NEW trials."""
    rng = np.random.default_rng(seed)
    seen, out = {_sha(spec)}, []

    def jitter(node):
        if isinstance(node, list):
            items = list(node)
            if len(items) == 2 and not any(isinstance(x, str) and x in TIME_FEATURES for x in items):
                for i, x in enumerate(items):
                    if isinstance(x, (int, float)) and not isinstance(x, bool):
                        items[i] = round(float(x) * float(rng.uniform(0.85, 1.15)), 4)
            return [jitter(x) for x in items]
        if isinstance(node, dict):
            return {k: (v if k == "prev" else jitter(v)) for k, v in node.items()}
        return node

    for k in range(n * 20):
        if len(out) >= n:
            break
        s = copy.deepcopy(spec)
        for ind in s.get("indicators", {}).values():
            if rng.random() < 0.5:
                ind["window"] = int(min(500, max(2, round(ind["window"] * float(rng.uniform(0.7, 1.4))))))
        for key in ("entry_long", "entry_short", "exit_long", "exit_short"):
            if key in s:
                s[key] = jitter(s[key])
        s["risk"]["stop_loss_pct"] = float(rng.choice([0.5, 1.0, 2.0, 3.0]))
        s["risk"]["take_profit_pct"] = rng.choice([None, 2.0, 4.0])
        s["risk"]["take_profit_pct"] = None if s["risk"]["take_profit_pct"] is None else float(s["risk"]["take_profit_pct"])
        if s["risk"].get("stop_atr_mult") is not None:
            s["risk"]["stop_atr_mult"] = round(float(rng.uniform(1.0, 3.5)), 2)
        s["name"] = f"{spec.get('name', 'spec')}_v{len(out) + 1}"
        try:
            validate_spec(s)
        except SpecError:
            continue
        h = _sha({**s, "name": ""})
        if h in seen or _sha({**spec, "name": ""}) == h:
            continue
        seen.add(h)
        out.append(s)
    return out
