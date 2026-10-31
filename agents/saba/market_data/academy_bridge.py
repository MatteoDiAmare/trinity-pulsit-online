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
COMMANDS = ("status", "start_cycle", "finish_cycle", "register_hypothesis", "run_hypothesis", "propose_next", "variants",
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


def _experiment(item, strategies_dir) -> dict:
    if not isinstance(item, dict) or not isinstance(item.get("label"), str) or not item["label"].strip():
        raise ValueError("each experiment needs a label")
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
        if cmd == "status":
            return {"ok": True, **orch.status(), "test_windows_consumed_until": orch.test_windows(), "instruments": sorted(available_instruments())}
        if cmd == "start_cycle":
            return {"ok": True, **orch.start_cycle()}
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
            return {"ok": True, "hypothesis_id": hid, "cells": len(plan["experiments"]) * len(instruments),
                    "preregistered_sha256": memory.hypothesis(hid)["preregistered_sha256"]}
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
            return {"ok": True, "lesson_id": lid}
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
            if status not in ("supported", "refuted", "abandoned"):
                raise ValueError("status must be supported, refuted or abandoned")
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
        out = handle(json.load(sys.stdin), root=args.root, strategies_dir=args.strategies_dir, owner_id=args.owner_id, org_id=args.org_id)
        print(json.dumps(jsonable(out), allow_nan=False))
        return 0
    except (ValueError, TypeError, KeyError, OSError, CycleDenied, json.JSONDecodeError, FileNotFoundError) as exc:
        print(json.dumps({"ok": False, "error": str(exc), "execution": "DISABLED"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
