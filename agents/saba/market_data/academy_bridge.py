"""JSON tool bridge so Saba (a Trinity agent) can run her research cycle. Research only: no broker, no orders.

Invoke as a subprocess: one JSON object on stdin, one JSON object on stdout.
  python -m market_data.academy_bridge --root <data volume> [--strategies-dir strategies] [--owner-id ...]
Commands (a fixed allowlist; nothing is executed from the request, no shell, no file paths except
`spec_file` names inside the strategies directory):
  status, start_cycle, finish_cycle, register_hypothesis, run_hypothesis, propose_next, variants,
  add_lesson, search_lessons, verify_lesson, notebook, final_test, conclude_hypothesis, halt,
  run_study, freeze_candidate, forward_report, test_strategy, refresh_data, help
Every call (request, answer, outcome, duration) is also appended to a tamper-evident audit log on the data volume (academy/audit.py).
`resume` is NOT available here: only the operator lifts a halt (academy_cli resume).
Exit code 0 on success; 1 with {"ok": false, "error": ...} otherwise.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import re
import sys
import time
import warnings
from pathlib import Path

from memory.experience_store import ExperienceStore
from .archive import MarketArchive, completed_bars_provider
from .laboratory import ExperimentRegistry
from .academy.experiments import run_final_test
from .academy.audit import AuditLog
from .academy.instruments import available_instruments, load_instrument
from .academy.ledger import LedgerStore, jsonable
from .academy.sessions import is_intraday
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
                            "run_in_cycle": "<cycle_id: registers AND runs in one call>", "parent_id": "optional",
                            "study_only": "optional true: use experiments [] when run_study (with this hypothesis_id) is the evidence"},
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
                  "train_bars": 750, "test_bars": 125, "code_file": "optional: your own strategy file instead of strategy/base_params (grid paths then start with params.)",
                  "intraday (60m etc.)": "interval '60m', fold_mode 'days', train_days 180, test_days 30; allow_overnight false by default (Saba's intraday-only policy); cost_multiplier 2 = double costs"},
                  "workers": 2, "hypothesis_id": "optional: link the study to this hypothesis (no extra trials are charged to it)"},
    "freeze_candidate": {"command": "freeze_candidate", "label": "sma 10/40 on 3 markets", "strategy": "sma_cross", "params": {"short": 10, "long": 40, "stop_loss_pct": 2.0},
                         "instruments": ["GOLD", "EURUSD", "SP500"], "interval": "1d",
                         "forecast": {"p_positive": 0.45, "expected_hit_rate": 0.42, "expected_expectancy": 0.5}, "rules": {"min_trades": 30, "min_days": 60},
                         "study_registry_id": "registry_id of the study this plan comes from. Only a study with verdict 'candidate' makes the entry a qualified candidate; "
                                              "without it (or after no_evidence) it is stored as purpose 'calibration': followed only to score your forecasts"},
    "forward_report": {"command": "forward_report", "update": True, "fetch": False},
    "refresh_data": {"command": "refresh_data"},
}
COMMANDS = ("refresh_data", "run_study", "freeze_candidate", "forward_report", "help", "test_strategy", "status", "start_cycle", "finish_cycle", "register_hypothesis", "run_hypothesis", "propose_next", "variants",
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


def _has_data(archive, instrument_id, interval) -> bool:
    try:
        archive.read(load_instrument(instrument_id).symbol, interval)
        return True
    except FileNotFoundError:
        return False


def _study_guidance(out: dict) -> str:
    v = out.get("verdict")
    if v == "candidate":
        return ("verdict 'candidate': every pre-registered criterion was met. Record it as a lesson with verdict 'candidate' (never 'supported') and freeze it "
                "with study_registry_id so that it is followed forward; remember that selection bias remains.")
    return ("verdict 'no_evidence': the search found nothing it could tell from luck. That is absence of evidence, not proof of absence. Record the lesson "
            "as 'inconclusive' and state what this study could and could not detect (markets, years, trades); 'refuted' is refused for a no_evidence study. "
            "If you still freeze a plan to score your own forecast, it is stored as purpose 'calibration', not as a candidate.")


def _freeze_purpose(registry, req: dict, item: dict):
    """A frozen plan is a qualified `candidate` only when it comes from a registered study with verdict 'candidate' and is one of that study's cells."""
    rid = req.get("study_registry_id")
    wanted = req.get("purpose")
    if wanted not in (None, "candidate", "calibration"):
        raise ValueError("purpose must be 'candidate' or 'calibration' (it is derived from study_registry_id; you normally omit it)")
    if rid is None:
        if wanted == "candidate":
            raise ValueError("purpose 'candidate' needs study_registry_id: the registry id of a study with verdict 'candidate'")
        return "calibration", req.get("study_sha256"), " | calibration: no qualifying study linked"
    if not isinstance(rid, int) or isinstance(rid, bool):
        raise ValueError("study_registry_id must be an integer")
    row = registry.db.execute("SELECT name, configuration_json, results_json FROM experiments WHERE id=?", (rid,)).fetchone()
    if row is None or row[0] != "academy:study":
        raise ValueError(f"study_registry_id {rid} is not a study in the registry")
    cfg, res = json.loads(row[1]), json.loads(row[2])
    if wanted == "calibration" or res.get("verdict") != "candidate":
        if wanted == "candidate":
            raise ValueError(f"study {rid} has verdict '{res.get('verdict')}', not 'candidate': it cannot make a qualified candidate")
        return "calibration", res.get("study_sha256"), f" | calibration: study {rid} verdict {res.get('verdict')}"
    sp = cfg["spec"]
    if item["strategy"] != sp.get("strategy"):
        raise ValueError(f"the plan's strategy {item['strategy']!r} is not the study's ({sp.get('strategy')!r})")
    if not set(req.get("instruments") or []) <= set(sp["instruments"]) or not req.get("instruments"):
        raise ValueError(f"instruments must be a non-empty subset of the study's {sp['instruments']}")
    if item["strategy"] != "sandboxed":
        base, grid, params = sp.get("base_params") or {}, sp.get("grid") or {}, item["params"]
        bad = [k for k, v in base.items() if k not in grid and params.get(k) != v] + [k for k, vals in grid.items() if params.get(k) not in vals]
        if bad:
            raise ValueError(f"parameters {bad} are not one of the study's cells")
    return "candidate", res.get("study_sha256"), f" | candidate: study {rid}"


def _refresh_data(archive, study_archive) -> dict:
    """Append the latest COMPLETED daily bars (Yahoo Finance, free; the forming current day is left out) to the archives that already hold each
    instrument. Never replaces history."""
    out = {}
    provider = completed_bars_provider()
    for iid, spec in sorted(available_instruments().items()):
        entry = {}
        for label, arc in (("standard", archive), ("extended", study_archive if study_archive is not archive else None)):
            if arc is None or not _has_data(arc, iid, "1d"):
                continue
            try:
                info = arc.ingest(spec.symbol, "1d", period="1mo", provider=provider)
                kept = {str(d.date()) for d in arc.read(spec.symbol, "1d").index}
                rejected = sorted(set(provider.seen.get(spec.symbol, [])) - kept)          # malformed provider bars (e.g. Low above Close) are not archived
                entry[label] = {"last_bar": info["last_timestamp_utc"][:10], "bars": info["row_count"], **({"rejected_bars": rejected} if rejected else {})}
            except Exception as exc:                  # network problems must not fail the call; report them
                entry[label] = {"error": str(exc)[:160]}
        out[iid] = entry
    return {"refreshed": out, "source": "Yahoo Finance via yfinance (free, daily)"}


def handle(req: dict, *, root, strategies_dir, owner_id="local", org_id="personal", extended_root=None, intraday_root=None) -> dict:
    if not isinstance(req, dict) or req.get("command") not in COMMANDS:
        raise ValueError(f"command must be one of {COMMANDS}")
    cmd = req["command"]
    root = Path(root).expanduser()
    archive = MarketArchive(root)
    registry = ExperimentRegistry(Path(archive.root) / "experiments.sqlite3")
    journal = ExperienceStore(str(Path(archive.root) / "academy_events.sqlite3"))
    memory = ResearchMemory(Path(archive.root) / "academy_memory.sqlite3", journal=journal)
    # Studies read the extended (longer-history) archive when it exists; the standard archive stays untouched so earlier experiments, splits and
    # forward candidates keep reproducing exactly. Both are refreshed by `refresh_data`.
    ext = Path(extended_root).expanduser() if extended_root else Path(archive.root).parent / "market_data_extended"
    study_archive = MarketArchive(ext) if ext.is_dir() else archive
    # Intraday studies read their OWN archive (never the daily ones, so no earlier experiment, split or forward candidate can change).
    intra = Path(intraday_root).expanduser() if intraday_root else Path(archive.root).parent / "market_data_intraday"
    intraday_archive = MarketArchive(intra) if intra.is_dir() else None
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
            cycle_id = _int(req, "cycle_id")
            hid = _int(req, "hypothesis_id", optional=True)
            if hid is not None:
                hyp = memory.hypothesis(hid)
                if hyp is None:
                    raise ValueError("unknown hypothesis_id")
                if hyp["status"] not in ("open", "testing"):
                    raise ValueError(f"hypothesis {hid} is {hyp['status']}; register a new (child) hypothesis instead")
            # the cycle budget is charged once, and only when the study is really computed: an identical study is returned from the registry
            if is_intraday(spec.interval):
                if intraday_archive is None:
                    raise ValueError("no intraday archive is installed (market_data_intraday); intraday studies need one")
                use = intraday_archive
            else:
                use = study_archive
            out = run_recorded_study(registry, use, spec, workers=min(max(int(req.get("workers", 1)), 1), 2),
                                     before_run=lambda: orch.charge(cycle_id, 1))
            out["cell_table"] = sorted([c for c in out["cell_table"] if c.get("valid")], key=lambda c: -(c.get("oos_sharpe_annualized") or -9))[:8]
            out["data_archive"] = "intraday" if use is intraday_archive else ("extended" if study_archive is not archive else "standard")
            if hid is not None:
                # evidence link only (idempotent). The study's cells are counted once, in the global trial index; the hypothesis' own
                # trial budget is deliberately NOT charged, so nothing is counted twice.
                memory.link_experiments(hid, [out["registry_id"]])
                if memory.hypothesis(hid)["status"] == "open":
                    memory.set_status(hid, "testing")
                out["hypothesis_id"] = hid
            out["how_to_record"] = _study_guidance(out)
            return {"ok": True, **out}
        if cmd == "freeze_candidate":
            from .academy.forward import ForwardTracker
            if is_intraday(str(req.get("interval", "1d"))):
                raise ValueError("forward tracking of intraday plans is not implemented yet (frozen candidates are daily); keep the intraday result as a lesson")
            item = _experiment({"label": _str(req, "label", maxlen=120), **{k: req[k] for k in ("strategy", "params", "spec", "spec_file", "code", "code_file", "seed", "limits") if k in req}}, sdir)
            purpose, study_sha, note = _freeze_purpose(registry, req, item)
            tr = ForwardTracker(Path(archive.root) / "academy_memory.sqlite3")
            try:
                fid = tr.freeze(archive, label=item["label"],
                                strategy=item["strategy"], params=item["params"], instruments=req.get("instruments") or [],
                                interval=req.get("interval", "1d"), forecast=req.get("forecast"), rules=req.get("rules"), study_sha256=study_sha,
                                note=(str(req.get("note", ""))[:200] + note)[:400], purpose=purpose)
            finally:
                tr.close()
            msg = ("qualified candidate: the linked study met every candidate criterion" if purpose == "candidate" else
                   "stored for forecast CALIBRATION only: it did not come from a study with verdict 'candidate', so it is not a candidate "
                   "(it is followed so that your forecast can be scored)")
            return {"ok": True, "frozen_id": fid, "purpose": purpose, "meaning": msg, "note": "the plan, data cutoff, rules and your forecast are now immutable"}
        if cmd == "forward_report":
            from .academy.forward import ForwardTracker
            tr = ForwardTracker(Path(archive.root) / "academy_memory.sqlite3")
            try:
                snaps = tr.update(archive, fetch=bool(req.get("fetch"))) if req.get("update", True) else []
                return {"ok": True, "snapshots": snaps, "forecast_report": tr.forecast_report()}
            finally:
                tr.close()
        if cmd == "refresh_data":
            return {"ok": True, **_refresh_data(archive, study_archive)}
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
            return {"ok": True, **orch.status(), "test_windows_consumed_until": orch.test_windows(), "instruments": sorted(available_instruments()),
                    "study_data": "extended (long history)" if study_archive is not archive else "standard",
                    "intraday_data": "installed" if intraday_archive is not None else "not installed"}
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
            study_only = req.get("study_only") is True
            if not isinstance(exps, list) or (not exps and not study_only):
                raise ValueError("experiments must be a non-empty list (or [] together with study_only: true when run_study is the evidence)")
            if exps and study_only:
                raise ValueError("study_only hypotheses have no experiments of their own: use experiments [] or drop study_only")
            labels = [e.get("label") if isinstance(e, dict) else None for e in exps]
            if len(set(labels)) != len(labels):
                raise ValueError("experiment labels must be unique")
            instruments = req.get("instruments")
            if not isinstance(instruments, list) or not set(instruments) <= set(available_instruments()) or not instruments:
                raise ValueError(f"instruments must be a non-empty subset of {sorted(available_instruments())}")
            plan = {"experiments": [_experiment(e, sdir) for e in exps], "instruments": instruments, "interval": req.get("interval", "1d")}
            if study_only:
                plan["study_only"] = True
            else:                       # run_hypothesis reads the standard archive: refuse instruments it does not hold instead of failing mid-run
                missing = [i for i in instruments if not _has_data(archive, i, plan["interval"])]
                if missing:
                    raise ValueError(f"no archived {plan['interval']} data in the standard archive for {missing}; use run_study (extended data) for these")
            hid = memory.register_hypothesis(_str(req, "statement"), _str(req, "family", maxlen=80), _int(req, "trial_budget"), plan,
                                             _int(req, "parent_id", optional=True))
            out = {"ok": True, "hypothesis_id": hid, "cells": len(plan["experiments"]) * len(instruments), "study_only": study_only,
                   "preregistered_sha256": memory.hypothesis(hid)["preregistered_sha256"]}
            if req.get("run_in_cycle") is not None and study_only:
                out["run_error"] = "study_only hypothesis: nothing to run here; call run_study with this hypothesis_id"
            elif req.get("run_in_cycle") is not None:
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
    warnings.filterwarnings("ignore")                       # yfinance deprecation notices must not pollute the JSON channel
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    p = argparse.ArgumentParser(description="Saba Academy research bridge (no orders)")
    p.add_argument("--root", required=True, help="Persistent data volume (archive root)")
    p.add_argument("--strategies-dir", default="strategies")
    p.add_argument("--owner-id", default="local")
    p.add_argument("--org-id", default="personal")
    p.add_argument("--extended-root", default=None, help="Long-history archive used by studies (default: <root>/../market_data_extended if it exists)")
    p.add_argument("--intraday-root", default=None, help="Intraday archive used by intraday studies (default: <root>/../market_data_intraday if it exists)")
    args = p.parse_args(argv)
    started = time.monotonic()
    request, req_cmd, rc = None, None, 1
    try:
        request = json.load(sys.stdin)
        req_cmd = request.get("command") if isinstance(request, dict) else None
        with contextlib.redirect_stderr(io.StringIO()):         # library warnings (yfinance / pandas) must not reach the caller; errors travel in the JSON reply
            out = handle(request, root=args.root, strategies_dir=args.strategies_dir, owner_id=args.owner_id, org_id=args.org_id,
                         extended_root=args.extended_root, intraday_root=args.intraday_root)
        reply, rc = jsonable(out), 0
        print(json.dumps(reply, allow_nan=False))
    except (ValueError, TypeError, KeyError, OSError, CycleDenied, json.JSONDecodeError, FileNotFoundError) as exc:
        reply = {"ok": False, "error": str(exc), "execution": "DISABLED"}
        if req_cmd in USAGE:
            reply["usage"] = USAGE[req_cmd]            # exact shape, so the caller does not have to guess field names
        print(json.dumps(reply))
    _audit(args.root, req_cmd, request, reply if "reply" in locals() else {"ok": False, "error": "no reply"}, rc == 0, started)
    return rc


def _audit(root, command, request, reply, ok, started):
    """Append this call to the durable audit log. Failure to record is reported on stderr and never changes the reply."""
    try:
        log = AuditLog(Path(MarketArchive(root).root) / "academy_audit.sqlite3")
        try:
            log.append(command=command, request=request, response=reply, ok=ok, duration_ms=int((time.monotonic() - started) * 1000))
        finally:
            log.close()
    except Exception as exc:                               # pragma: no cover - last-resort safety
        print(f"audit log not written: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
