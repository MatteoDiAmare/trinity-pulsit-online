#!/usr/bin/env python3
"""Controlled-installation checks for Saba's Academy (read-only against the container; the install itself is integration/deploy_academy_to_container.sh).

  saba_install_check.py snapshot [--out DIR]          back up everything the install must preserve (data volumes, strategies, versions, container start times)
  saba_install_check.py reproduce SNAPSHOT [--workers 2]
        recompute every recorded daily study with the CURRENT code against the snapshot's own data and compare with the stored result and dataset hashes;
        re-verify every lesson's evidence hash. Proves the new code reproduces the old research.
  saba_install_check.py verify SNAPSHOT [--allow-added] [--allow-data-refresh]
        compare the live container with the snapshot after an install: every old database row identical, no archive bar changed, other containers not restarted,
        no schedule, budget unchanged, code version reported.

Everything is copied with docker cp / docker exec; nothing is written into the container, no secret is read or printed. Snapshots are mode 700 on the host.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
HERE = Path(__file__).resolve().parent
CONTAINER = "agent-saba"
BASE = "/home/developer/.local/share/saba"
DIRS = ("market_data", "market_data_extended")
DBS = ("experiments.sqlite3", "academy_memory.sqlite3", "academy_events.sqlite3")
GROWS = ("academy_audit.sqlite3",)                # the audit log is append-only and grows with every bridge call by design


def run(*a, timeout=300, check=True):
    r = subprocess.run(list(a), capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise SystemExit(f"{' '.join(a[:4])}: {r.stderr.strip()[:300]}")
    return r.stdout


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def containers() -> dict:
    out = run("docker", "ps", "--format", "{{.Names}}")
    res = {}
    for n in out.split():
        res[n] = run("docker", "inspect", n, "--format", "{{.State.StartedAt}}").strip()
    return res


def trinity_get(path):
    r = subprocess.run([str(HERE / "trinity_api.sh"), "GET", path], capture_output=True, text=True, timeout=60)
    return json.loads(r.stdout) if r.returncode == 0 else None


def live_facts() -> dict:
    ver = run("docker", "exec", CONTAINER, "bash", "-lc", f"cat {BASE}/academy/ACADEMY_CODE_VERSION 2>/dev/null; echo; cd {BASE}/academy && {BASE}/venv/bin/python -c "
              "\"from market_data.academy.orchestrator import Budget; import dataclasses, json; print(json.dumps(dataclasses.asdict(Budget())))\"").strip().splitlines()
    sched = trinity_get(f"/api/agents/saba/schedules")
    guard = run("docker", "inspect", CONTAINER, "--format", "{{range .Config.Env}}{{println .}}{{end}}").splitlines()
    return {"code_version": ver[0] if ver else None, "budget": json.loads(ver[-1]) if ver else None, "schedules": None if sched is None else len(sched),
            "guardrails": next((g for g in guard if g.startswith("AGENT_GUARDRAILS=")), None), "containers": containers()}


def copy_out(dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    for d in DIRS:
        run("docker", "cp", f"{CONTAINER}:{BASE}/{d}", str(dest / d), timeout=600)
    run("docker", "cp", f"{CONTAINER}:/home/developer/strategies", str(dest / "strategies"), check=False)


def manifest(root: Path) -> dict:
    return {str(p.relative_to(root)): sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


def snapshot(a):
    out = Path(a.out).expanduser() / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(out.parent, 0o700)
    copy_out(out)
    for p in [out, *out.rglob("*")]:
        os.chmod(p, 0o700 if p.is_dir() else 0o600)
    meta = {"taken_at_utc": datetime.now(timezone.utc).isoformat(), **live_facts(), "files": manifest(out)}
    (out / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True))
    os.chmod(out / "meta.json", 0o600)
    print(json.dumps({"snapshot": str(out), "files": len(meta["files"]), "code_version": meta["code_version"], "schedules": meta["schedules"], "budget": meta["budget"]}, indent=1))


def rows(db: Path, table: str) -> set:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return {json.dumps(dict(r), sort_keys=True, default=str) for r in c.execute(f'SELECT * FROM "{table}"')}


def tables(db: Path) -> list:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    return [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%fts%'")]


def verify(a):
    snap = Path(a.snapshot).expanduser()
    meta = json.loads((snap / "meta.json").read_text())
    problems, notes = [], []
    bad = [f for f, h in meta["files"].items() if (snap / f).exists() and sha(snap / f) != h]
    if bad:
        problems.append(f"the snapshot itself was modified: {bad[:3]}")
    with tempfile.TemporaryDirectory() as tmp:
        live = Path(tmp)
        copy_out(live)
        # 1. databases: every old row identical
        report = {}
        for dbn in DBS:
            old, new = snap / "market_data" / dbn, live / "market_data" / dbn
            for t in tables(old):
                a_rows, b_rows = rows(old, t), rows(new, t)
                changed, added = len(a_rows - b_rows), len(b_rows - a_rows)
                report[f"{dbn}:{t}"] = {"before": len(a_rows), "after": len(b_rows), "changed_or_missing": changed, "added": added}
                if changed:
                    problems.append(f"{dbn}:{t}: {changed} old row(s) changed or missing")
                if added and not a.allow_added:
                    problems.append(f"{dbn}:{t}: {added} new row(s) (use --allow-added if intended)")
        # 2. archives: no bar may change
        import pandas as pd
        arch = {"identical": 0, "appended": 0}
        for d in DIRS:
            for pq in (snap / d).rglob("bars.parquet"):
                rel = pq.relative_to(snap)
                new = live / rel
                if not new.exists():
                    problems.append(f"archive file missing: {rel}")
                elif sha(pq) == sha(new):
                    arch["identical"] += 1
                else:
                    o, n = pd.read_parquet(pq), pd.read_parquet(new)
                    same = o.index.isin(n.index).all() and (n.loc[o.index].round(8).equals(o.round(8)))
                    if same and a.allow_data_refresh:
                        arch["appended"] += 1
                    else:
                        problems.append(f"archive changed: {rel} ({'bars appended' if same else 'EXISTING BARS DIFFER'})")
        new_dirs = sorted(set(p.name for p in live.glob("market_data*")) - set(DIRS))
        # 3. platform
        now = live_facts()
        for n, started in meta["containers"].items():
            if now["containers"].get(n) != started:
                problems.append(f"container {n} was restarted or is gone")
        if now["schedules"] not in (0, None) or meta["schedules"] not in (0, None):
            problems.append(f"schedules present: before {meta['schedules']}, after {now['schedules']}")
        if now["budget"] != meta["budget"]:
            problems.append(f"budget changed: {meta['budget']} -> {now['budget']}")
        if now["guardrails"] != meta["guardrails"]:
            problems.append("AGENT_GUARDRAILS changed")
        strat_old, strat_new = {k: v for k, v in meta["files"].items() if k.startswith("strategies/")}, manifest(live).copy()
        strat_new = {k: v for k, v in strat_new.items() if k.startswith("strategies/")}
        if any(strat_new.get(k) != v for k, v in strat_old.items()):
            problems.append("a strategy file differs from the snapshot")
    print(json.dumps({"ok": not problems, "problems": problems, "code_version_before": meta["code_version"], "code_version_after": now["code_version"],
                      "archives": arch, "budget": now["budget"], "schedules": now["schedules"], "tables": len(report),
                      "rows_added": sum(v["added"] for v in report.values()), "rows_changed": sum(v["changed_or_missing"] for v in report.values())}, indent=1))
    return 0 if not problems else 1


def reproduce_experiments(snap, reg, np):
    """Recompute every recorded single experiment (built-in, JSON-spec and sandboxed Python strategies) on its own train and validation window with the CURRENT code
    and compare every metric with what was stored. Also checks that the instrument spec still hashes to the recorded value."""
    from market_data.archive import MarketArchive
    from market_data.academy.experiments import evaluate_segment
    from market_data.academy.instruments import load_instrument
    from market_data.academy.integrity import dataset_sha256
    from market_data.academy.simulator import SimConfig
    rep = {"recomputed": 0, "skipped": [], "mismatches": [], "spec_hash_ok": 0}
    arch = MarketArchive(snap / "market_data")
    for eid, name, cfg_j, res_j in reg.db.execute("SELECT id, name, configuration_json, results_json FROM experiments WHERE name LIKE 'academy:%' AND name NOT IN ('academy:study','academy:train_summary','academy:final_test') ORDER BY id").fetchall():
        cfg, res = json.loads(cfg_j), json.loads(res_j)
        if "train" not in res or "sim_config" not in cfg:
            rep["skipped"].append((eid, name, "no recomputable record"))
            continue
        if cfg.get("instrument_id"):
            if load_instrument(cfg["instrument_id"]).spec_sha256 == cfg["instrument_spec_sha256"]:
                rep["spec_hash_ok"] += 1
            else:
                rep["mismatches"].append({"id": eid, "problem": "instrument spec no longer hashes to the recorded value"})
        sp = cfg["split"]
        bars = arch.read(cfg["symbol"], cfg["interval"])
        dev = bars.iloc[:sp["val_end_index"]]
        if dataset_sha256(dev) != cfg["dev_dataset_sha256"]:
            rep["mismatches"].append({"id": eid, "problem": "development data differs from the recorded hash"})
            continue
        sim = SimConfig.from_dict(cfg["sim_config"])
        spec = {"strategy": cfg["strategy"], "params": cfg["params"]}
        for part, (lo, hi) in (("train", (0, sp["train_end_index"])), ("validation", (sp["train_end_index"], sp["val_end_index"]))):
            new = evaluate_segment(dev, spec, sim, lo, hi, cfg["interval"])
            for k, old_v in res[part].items():
                nv = new.get(k)
                same = (np.isclose(old_v, nv, rtol=1e-9, atol=1e-9) if isinstance(old_v, (int, float)) and isinstance(nv, (int, float)) and not isinstance(old_v, bool) else old_v == nv)
                if not same:
                    rep["mismatches"].append({"id": eid, "name": name, "part": part, "metric": k, "stored": old_v, "now": nv})
        rep["recomputed"] += 1
    return rep


def reproduce(a):
    import numpy as np
    from market_data.archive import MarketArchive
    from market_data.laboratory import ExperimentRegistry
    from market_data.academy.integrity import dataset_sha256
    from market_data.academy.instruments import load_instrument
    from market_data.academy.memory import ResearchMemory
    from market_data.academy.study import StudySpec, run_study
    snap = Path(a.snapshot).expanduser()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for item in ("experiments.sqlite3", "academy_memory.sqlite3", "academy_events.sqlite3"):       # copies: the recomputation must not touch the snapshot
            (work / item).write_bytes((snap / "market_data" / item).read_bytes())
        reg = ExperimentRegistry(work / "experiments.sqlite3")
        studies = reg.db.execute("SELECT id, configuration_json, results_json FROM experiments WHERE name='academy:study' ORDER BY id").fetchall()
        out = {"studies": [], "lessons": None}
        skip = {"spec_sha256", "fold_mode", "pooling", "cost_multiplier", "bars_per_year_used", "data_quality", "excluded_markets", "glitch_bars_removed", "reused", "registry_id", "study_sha256", "prior_trials_counted", "intraday", "data_archive", "how_to_record"}
        for rid, cfg_j, res_j in studies:
            cfg, stored = json.loads(cfg_j), json.loads(res_j)
            # A record written before the breadth work has no fold_mode/pooling/clean fields: it was produced with bar-index folds, fixed pooling and no
            # cleaning, so those are the settings that reproduce it (the same switches that reproduced the three published studies exactly).
            legacy = {} if "fold_mode" in cfg["spec"] else {"fold_mode": "bars", "pooling": "fixed", "clean_data": False, "respect_usable_from": False}
            spec = StudySpec.from_dict({**cfg["spec"], **legacy, "prior_trials": cfg["prior_trials"]})
            row = {"registry_id": rid, "name": spec.name, "markets": spec.instruments, "recorded_by_legacy_engine": bool(legacy)}
            # which archive did it run on? the one whose prefix hashes match the recorded dataset hashes
            bars, dev_end, hash_ok = {}, cfg["dev_end"], True
            for root in ("market_data", "market_data_extended"):
                arch = MarketArchive(snap / root)
                try:
                    cand = {i: arch.read(load_instrument(i).symbol, spec.interval) for i in spec.instruments}
                except FileNotFoundError:
                    continue
                if all(dataset_sha256(cand[i].iloc[:dev_end[i]]) == cfg["dev_dataset_sha256"][i] for i in spec.instruments):
                    bars, row["archive"] = cand, root
                    break
            if not bars:
                row.update(ok=False, error="no archive in the snapshot reproduces the recorded dataset hashes")
                out["studies"].append(row)
                continue
            new = run_study(spec, bars, dev_end, workers=a.workers)
            diffs, new_fields = [], []

            def cmp(path, x, y):
                if isinstance(x, dict) and isinstance(y, dict):
                    for k in set(x) | set(y):
                        if k in skip and not path:
                            continue
                        if k not in x:                       # a field the old engine did not write: reported, not a difference
                            new_fields.append(f"{path}.{k}" if path else k)
                            continue
                        cmp(f"{path}.{k}" if path else k, x.get(k), y.get(k))
                elif isinstance(x, list) and isinstance(y, list):
                    if len(x) != len(y):
                        diffs.append(f"{path}: length {len(x)} vs {len(y)}")
                    for i, (p, q) in enumerate(zip(x, y)):
                        cmp(f"{path}[{i}]", p, q)
                elif isinstance(x, (int, float)) and isinstance(y, (int, float)) and not isinstance(x, bool):
                    if not np.isclose(x, y, rtol=1e-9, atol=1e-9):
                        diffs.append(f"{path}: {x} vs {y}")
                elif x != y:
                    diffs.append(f"{path}: {str(x)[:40]} vs {str(y)[:40]}")
            cmp("", json.loads(json.dumps(stored, default=str)), json.loads(json.dumps(new, default=str)))
            row.update(ok=not diffs, differences=diffs[:40], fields_added_since=sorted(new_fields), verdict=new["verdict"], sharpe=new["wfo"]["sharpe_annualized"], trades=new["wfo"]["trades"], dataset_hashes_match=True)
            out["studies"].append(row)
        out["experiments"] = reproduce_experiments(snap, reg, np)
        mem = ResearchMemory(work / "academy_memory.sqlite3")
        lessons = [r[0] for r in mem.db.execute("SELECT id FROM lessons ORDER BY id")]
        checks = [mem.verify_lesson(reg, lid) for lid in lessons]
        out["lessons"] = {"count": len(lessons), "all_evidence_hashes_ok": all(c["ok"] for c in checks), "failed": [c for c in checks if not c["ok"]]}
        mem.close()
        reg.close()
    out["ok"] = (bool(out["studies"]) and all(s["ok"] for s in out["studies"]) and out["lessons"]["all_evidence_hashes_ok"]
                 and out["experiments"]["mismatches"] == [] and out["experiments"]["recomputed"] > 0)
    print(json.dumps(out, indent=1, default=str))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--out", default="~/.local/share/saba-audit/saba-install-snapshots")
    r = sub.add_parser("reproduce")
    r.add_argument("snapshot")
    r.add_argument("--workers", type=int, default=2)
    v = sub.add_parser("verify")
    v.add_argument("snapshot")
    v.add_argument("--allow-added", action="store_true")
    v.add_argument("--allow-data-refresh", action="store_true")
    a = p.parse_args()
    if a.cmd == "snapshot":
        snapshot(a)
    elif a.cmd == "reproduce":
        sys.exit(reproduce(a))
    else:
        sys.exit(verify(a))
