"""JSON tool bridge so Saba (a Trinity agent) can run her research cycle. Research only: no broker, no orders.

Invoke as a subprocess: one JSON object on stdin, one JSON object on stdout.
  python -m market_data.academy_bridge --root <data volume> [--strategies-dir strategies] [--owner-id ...]
Commands (a fixed allowlist; nothing is executed from the request, no shell, no file paths except
`spec_file` names inside the strategies directory):
  status, start_cycle, finish_cycle, register_hypothesis, run_hypothesis, propose_next, variants,
  add_lesson, search_lessons, verify_lesson, notebook, final_test, conclude_hypothesis, halt
`resume` is NOT available here: only the operator lifts a halt (academy_cli resume).
Exit code 0 on success; 1 with {"ok": false, "error": ...} otherwise.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from memory.experience_store import ExperienceStore
from .archive import MarketArchive
from .laboratory import ExperimentRegistry
from .academy.experiments import run_final_test
from .academy.instruments import available_instruments
from .academy.ledger import LedgerStore, jsonable
from .academy.memory import ResearchMemory
from .academy.orchestrator import CycleDenied, Orchestrator, mutate_spec
from .academy.spec_strategy import validate_spec
from .academy.strategies import STRATEGIES, build_strategy

FILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\.json$")
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\.py$")
USAGE = {
    "start_cycle": {"command": "start_cycle"},
    "register_hypothesis": {"command": "register_hypothesis", "statement": "text", "family": "trend", "trial_budget": 4,
                            "instruments": ["GOLD", "EURUSD"], "interval": "1d",
                            "experiments": [{"label": "a", "strategy": "sma_cross", "params": {"short": 10, "long": 30}},
                                            {"label": "b", "spec_file": "ema_trend.json"},
                                            {"label": "c", "code_file": "my_strategy.py", "params": {}}],
                            "run_in_cycle": "<cycle_id: registers AND runs in one call>", "parent_id": "optional"},
    "run_hypothesis": {"command": "run_hypothesis", "cycle_id": 1, "hypothesis_id": 1},
    "add_lesson": {"command": "add_lesson", "hypothesis_id": 1, "claim": "text", "verdict": "refuted|candidate|inconclusive|supported",
                   "confidence": 0.5, "evidence": [{"experiment_id": 3}], "scope": {"market": "GOLD", "interval": "1d"},
                   "tags": "optional", "conclude_hypothesis": "optional: refuted|inconclusive|abandoned|supported"},
    "conclude_hypothesis": {"command": "conclude_hypothesis", "hypothesis_id": 1, "status": "refuted|inconclusive|abandoned|supported (needs a supported lesson)"},
    "final_test": {"command": "final_test", "experiment_id": 3},
    "finish_cycle": {"command": "finish_cycle", "cycle_id": 1},
    "search_lessons": {"command": "search_lessons", "text": "optional", "verdict": "optional", "market": "optional", "limit": 20},
    "variants": {"command": "variants", "spec_file": "ema_trend.json", "n": 3, "seed": 1},
    "test_strategy": {"command": "test_strategy", "code_file": "my_strategy.py", "instrument": "GOLD", "params": {}},
    "halt": {"command": "halt", "reason": "text"},
    "run_study": {"command": "run_study", "cycle_id": 1, "study": {"name": "sma grid", "strategy": "sma_cross", "base_params": {"stop_loss_pct": 2.0},
                  "grid": {"short": [5, 10, 20], "long": [40, 80, 150]}, "instruments": ["GOLD", "EURUSD", "SP500"], "interval": "1d",
                  "train_bars": 750, "test_bars": 125, "code_file": "optional: your own strategy file instead of strategy/base_params (grid paths then start with params.)"}, "workers": 2},
    "freeze_candidate": {"command": "freeze_candidate", "label": "sma 10/40 on 3 markets", "strategy": "sma_cross", "params": {"short": 10, "long": 40, "stop_loss_pct": 2.0},
                         "instruments": ["GOLD", "EURUSD", "SP500"], "interval": "1d",
                         "forecast": {"p_positive": 0.45, "expected_hit_rate": 0.42, "expected_expectancy": 0.5}, "rules": {"min_trades": 30, "min_days": 60},
                         "study_sha256": "optional"},
    "forward_report": {"command": "forward_report", "update": True, "fetch": False},
}
COMMANDS = ("run_study", "freeze_candidate", "forward_report", "help", "test_strategy", "status", "start_cycle", "finish_cycle", "register_hypothesis", "run_hypothesis", "propose_next", "variants",
            "add_lesson", "search_lessons", "verify_lesson", "notebook", "final_test", "conclude_hypothesis", "halt")


def _int(req, key, optional=False):
    v = req.get(key)
    if v is None and optional:
        return None
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError(f"{key} must be an integer")
    return v


def _str(req, key, optional=False, maxlen=2000):
    v = req.get(key)
    if v is None and optional:
        return None
    if not isinstance(v, str) or not v.strip() or len(v) > maxlen:
        raise ValueError(f"{key} must be a non-empty string (max {maxlen} chars)")
    return v


def _load_spec(req, strategies_dir: Path) -> dict:
    if "spec_file" in req:
        name = req["spec_file"]
        if not isinstance(name, str) or not FILE_RE.fullmatch(name):
            raise ValueError("spec_file must be a plain file name like 'ema_trend.json'")
        path = strategies_dir / name
        if not path.is_file():
            raise ValueError(f"spec file {name} not found in the strategies directory")
        return validate_spec(json.loads(path.read_text(encoding="utf-8")))
    return validate_spec(req["spec"])


def _load_code(item, strategies_dir: Path) -> dict:
    """Strategy code written by Saba: a plain file name inside <strategies>/code/ (versioned in git) or inline text.
    It is only validated here; it runs exclusively inside the isolated worker."""
    if "code_file" in item:
        name = item["code_file"]
        if not isinstance(name, str) or not CODE_RE.fullmatch(name):
            raise ValueError("code_file must be a plain file name like 'momentum.py'")
        path = Path(strategies_dir) / "code" / name
        if not path.is_file():
            raise ValueError(f"code file {name} not found in <strategies>/code/")
        source = path.read_text(encoding="utf-8")
    else:
        source = item.get("code")
    params = {"code": source, "seed": item.get("seed", 0), "params": item.get("params") or {}}
    if item.get("limits"):
        params["limits"] = item["limits"]
    build_strategy("sandboxed", params)          # policy check only; starts no process
    return params


def _experiment(item, strategies_dir) -> dict:
    if not isinstance(item, dict) or not isinstance(item.get("label"), str) or not item["label"].strip():
        raise ValueError("each experiment needs a label")
    if "code_file" in item or "code" in item:
        return {"strategy": "sandboxed", "params": _load_code(item, strategies_dir), "label": item["label"]}
    if "spec" in item or "spec_file" in item:
        return {"strategy": "spec", "params": {"spec": _load_spec(item, strategies_dir)}, "label": item["label"]}
    name = item.get("strategy")
    if name not in STRATEGIES or name == "spec":
        raise ValueError(f"strategy must be one of {sorted(set(STRATEGIES) - {'spec'})} or a spec")
    build_strategy(name, item.get("params") or {})        # validates parameters (incl. mandatory stop) without running anything
    return {"strategy": name, "params": item.get("params") or {}, "label": item["label"]}


def handle(req: dict, *, root, strategies_dir, owner_id="local", org_id="personal") -> dict:
    if not isinstance(req, dict) or req.get("command") not in COMMANDS:
        raise ValueError(f"command must be one of {COMMANDS}")
    cmd = req["command"]
    root = Path(root).expanduser()
    archive = MarketArchive(root)
    registry = ExperimentRegistry(Path(archive.root) / "experiments.sqlite3")
    journal = ExperienceStore(str(Path(archive.root) / "academy_events.sqlite3"))
    memory = ResearchMemory(Path(archive.root) / "academy_memory.sqlite3", journal=journal)
    ledger = LedgerStore(archive.root, owner_id, org_id)
    orch = Orchestrator(archive.root, registry, archive, memory, ledger)
    sdir = Path(strategies_dir)
    try:
        if cmd == "run_study":
            from .academy.study import StudySpec, run_recorded_study
            study = req.get("study")
            if not isinstance(study, dict):
                raise ValueError("study must be an object")
            study = {k: v for k, v in study.items() if k != "max_cells"}
            if "code_file" in study:         # own code: the file is read from <strategies>/code and becomes the (sandboxed) base parameters
                code_params = _load_code({"code_file": study.pop("code_file"), "params": study.get("base_params", {})}, sdir)
                study["strategy"], study["base_params"] = "sandboxed", code_params
            spec = StudySpec.from_dict(study)
            orch.charge(_int(req, "cycle_id"), 1)
            out = run_recorded_study(registry, archive, spec, workers=min(max(int(req.get("workers", 1)), 1), 2))
            out["cell_table"] = sorted([c for c in out["cell_table"] if c.get("valid")], key=lambda c: -(c.get("oos_sharpe_annualized") or -9))[:8]
            return {"ok": True, **out}
        if cmd == "freeze_candidate":
            from .academy.forward import ForwardTracker
            item = _experiment({"label": _str(req, "label", maxlen=120), **{k: req[k] for k in ("strategy", "params", "spec", "spec_file", "code", "code_file", "seed", "limits") if k in req}}, sdir)
            tr = ForwardTracker(Path(archive.root) / "academy_memory.sqlite3")
            try:
                fid = tr.freeze(archive, label=item["label"], strategy=item["strategy"], params=item["params"], instruments=req.get("instruments") or [],
                                interval=req.get("interval", "1d"), forecast=req.get("forecast"), rules=req.get("rules"), study_sha256=req.get("study_sha256"),
                                note=str(req.get("note", ""))[:300])
            finally:
                tr.close()
            return {"ok": True, "frozen_id": fid, "note": "the plan, data cutoff, rules and your forecast are now immutable"}
        if cmd == "forward_report":
            from .academy.forward import ForwardTracker
            tr = ForwardTracker(Path(archive.root) / "academy_memory.sqlite3")
            try:
                snaps = tr.update(archive, fetch=bool(req.get("fetch"))) if req.get("update", True) else []
                return {"ok": True, "snapshots": snaps, "forecast_report": tr.forecast_report()}
            finally:
                tr.close()
        if cmd == "help":
            return {"ok": True, "commands": USAGE, "tips": "start_cycle returns a briefing (status, notebook, ranking). Combine steps: "
                    "register_hypothesis with run_in_cycle runs it at once; add_lesson with conclude_hypothesis closes the hypothesis. "
                    "Errors include the expected usage."}
        if cmd == "test_strategy":
            # dry run on the TRAIN portion only (first 60 %): policy, causality gate and a quick simulation. Records nothing.
            # Validation data is deliberately NOT shown here: tuning against it without counting trials would understate
            # the luck benchmark. Validation results come only from registered experiments, which are counted.
            from .academy.causality import check_causal
            from .academy.experiments import Split, evaluate_segment
            from .academy.instruments import load_instrument
            from .academy.simulator import SimConfig
            exp = _experiment({"label": "dry-run", **{k: v for k, v in req.items() if k in ("code", "code_file", "spec", "spec_file", "seed", "params", "limits")}}, sdir)
            spec = load_instrument(_str(req, "instrument", maxlen=20))
            interval = req.get("interval", "1d")
            bars = archive.read(spec.symbol, interval)
            split = Split.chronological(len(bars))
            train = bars.iloc[:split.train_end]
            cfg = SimConfig(1000.0, instrument=spec, max_exposure=None)
            gate = check_causal(exp["strategy"], exp["params"], train, cfg)
            out = {"ok": True, "policy": "passed", "causality": gate,
                   "data_used": f"train portion only: first {split.train_end} of {len(bars)} bars",
                   "note": "validation results are only available via registered experiments (each counted as a trial)"}
            if gate["ok"]:
                out["train"] = evaluate_segment(train, exp, cfg, 0, split.train_end, interval)
            return out
        if cmd == "status":
            return {"ok": True, **orch.status(), "test_windows_consumed_until": orch.test_windows(), "instruments": sorted(available_instruments())}
        if cmd == "start_cycle":
            started = orch.start_cycle()
            status = {**orch.status(), "test_windows_consumed_until": orch.test_windows()}
            return {"ok": True, **started, "briefing": {"status": status, "ranking": orch.propose_next(),
                                                        "notebook": memory.notebook(status=status, max_chars=5000),
                                                        "usage": "send {'command':'help'} for exact JSON of every command"}}
        if cmd == "finish_cycle":
            return {"ok": True, **orch.finish_cycle(_int(req, "cycle_id"))}
        if cmd == "register_hypothesis":
            exps = req.get("experiments")
            if not isinstance(exps, list) or not exps:
                raise ValueError("experiments must be a non-empty list")
            labels = [e.get("label") if isinstance(e, dict) else None for e in exps]
            if len(set(labels)) != len(labels):
                raise ValueError("experiment labels must be unique")
            instruments = req.get("instruments")
            if not isinstance(instruments, list) or not set(instruments) <= set(available_instruments()) or not instruments:
                raise ValueError(f"instruments must be a non-empty subset of {sorted(available_instruments())}")
            plan = {"experiments": [_experiment(e, sdir) for e in exps], "instruments": instruments, "interval": req.get("interval", "1d")}
            hid = memory.register_hypothesis(_str(req, "statement"), _str(req, "family", maxlen=80), _int(req, "trial_budget"), plan,
                                             _int(req, "parent_id", optional=True))
            out = {"ok": True, "hypothesis_id": hid, "cells": len(plan["experiments"]) * len(instruments),
                   "preregistered_sha256": memory.hypothesis(hid)["preregistered_sha256"]}
            if req.get("run_in_cycle") is not None:
                try:
                    out["run"] = orch.run_hypothesis(_int(req, "run_in_cycle"), hid)
                except (ValueError, CycleDenied) as exc:        # registration stands; the run can be retried or the plan dropped
                    out["run_error"] = str(exc)
            return out
        if cmd == "run_hypothesis":
            return {"ok": True, **orch.run_hypothesis(_int(req, "cycle_id"), _int(req, "hypothesis_id"))}
        if cmd == "propose_next":
            return {"ok": True, "ranking": orch.propose_next()}
        if cmd == "variants":
            n = _int(req, "n")
            if not 1 <= n <= 20:
                raise ValueError("n must be 1..20")
            return {"ok": True, "variants": mutate_spec(_load_spec(req, sdir), n, _int(req, "seed", optional=True) or 0)}
        if cmd == "add_lesson":
            lid = memory.add_lesson(registry, claim=_str(req, "claim"), verdict=_str(req, "verdict", maxlen=20),
                                    confidence=req.get("confidence"), evidence=req.get("evidence") or [], scope=req.get("scope") or {},
                                    hypothesis_id=_int(req, "hypothesis_id", optional=True), tags=req.get("tags") or "")
            out = {"ok": True, "lesson_id": lid}
            if req.get("conclude_hypothesis") is not None:
                if req.get("hypothesis_id") is None:
                    raise ValueError("conclude_hypothesis needs hypothesis_id")
                memory.set_status(_int(req, "hypothesis_id"), _str(req, "conclude_hypothesis", maxlen=20))
                out["hypothesis_status"] = req["conclude_hypothesis"]
            return out
        if cmd == "search_lessons":
            return {"ok": True, "lessons": memory.search(req.get("text"), verdict=req.get("verdict"), market=req.get("market"),
                                                         limit=req.get("limit", 20))}
        if cmd == "verify_lesson":
            return {"ok": True, **memory.verify_lesson(registry, _int(req, "lesson_id"))}
        if cmd == "notebook":
            status = {**orch.status(), "test_windows_consumed_until": orch.test_windows()}
            text = memory.notebook(status=status)
            if req.get("write"):
                memory.write_notebook(Path(archive.root) / "research_notebook.md", status=status)
            return {"ok": True, "notebook": text}
        if cmd == "conclude_hypothesis":
            status = _str(req, "status", maxlen=20)
            if status not in ("supported", "refuted", "inconclusive", "abandoned"):
                raise ValueError("status must be supported, refuted, inconclusive or abandoned")
            memory.set_status(_int(req, "hypothesis_id"), status)
            return {"ok": True}
        if cmd == "halt":
            orch.halt(_str(req, "reason", maxlen=500))
            return {"ok": True, "halted": True}
        # final_test: protected by the integrity rules (frontier, window budget, one test per experiment)
        eid = _int(req, "experiment_id")
        row = registry.db.execute("SELECT configuration_json FROM experiments WHERE id=?", (eid,)).fetchone()
        if row is None:
            raise ValueError("unknown experiment")
        cfg = json.loads(row[0])
        bars = archive.read(cfg["symbol"], cfg["interval"])
        out = run_final_test(bars, registry, eid, ledger=ledger)
        return {"ok": True, **out}
    finally:
        memory.close()
        journal.close()
        registry.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Saba Academy research bridge (no orders)")
    p.add_argument("--root", required=True, help="Persistent data volume (archive root)")
    p.add_argument("--strategies-dir", default="strategies")
    p.add_argument("--owner-id", default="local")
    p.add_argument("--org-id", default="personal")
    args = p.parse_args(argv)
    try:
        request = json.load(sys.stdin)
        req_cmd = request.get("command") if isinstance(request, dict) else None
        out = handle(request, root=args.root, strategies_dir=args.strategies_dir, owner_id=args.owner_id, org_id=args.org_id)
        print(json.dumps(jsonable(out), allow_nan=False))
        return 0
    except (ValueError, TypeError, KeyError, OSError, CycleDenied, json.JSONDecodeError, FileNotFoundError) as exc:
        reply = {"ok": False, "error": str(exc), "execution": "DISABLED"}
        cmd = locals().get("req_cmd")
        if cmd in USAGE:
            reply["usage"] = USAGE[cmd]            # exact shape, so the caller does not have to guess field names
        print(json.dumps(reply))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
