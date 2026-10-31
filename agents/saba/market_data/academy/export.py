"""Versioned static JSON export of SIMULATED research results (schema v1).

Layout (everything under a fixed `simulation/` namespace so it can never be mixed up with
future real holdings or bookkeeping data, which would live under a different top-level prefix):

  v1/simulation/orgs/<org_id>/users/<owner_id>/
      manifest.json                      index + sha256 of every file
      strategies.json
      training_runs.json
      experiments/<id>/summary.json
      experiments/<id>/<period>/{trades,positions,equity,decisions,risk}.json

These files are NOT public. They are meant for a private Supabase Storage bucket guarded by
Supabase Auth (see ACADEMY_EXPORT.md and sql/). The exporter never uploads anything itself.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .ledger import LedgerStore, check_id, jsonable
from .strategies import STRATEGIES

SCHEMA_VERSION = "1.0"
API_PREFIX = "v1"
DOMAIN = "simulation"
DISCLAIMER = ("Simulated historical results. No real orders, positions or holdings. "
              "Not investment advice.")
MAX_EQUITY_POINTS = 2000
DATA_FILES = ("trades", "positions", "equity", "decisions", "risk")


def _repo_root(path: Path) -> Path | None:
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _downsample(rows: list, limit: int) -> tuple[list, bool]:
    if len(rows) <= limit:
        return rows, False
    step = len(rows) / (limit - 1)
    idx = sorted({int(i * step) for i in range(limit - 1)} | {len(rows) - 1})
    return [rows[i] for i in idx], True


def _envelope(kind: str, store: LedgerStore, experiment_id, period, data, **extra) -> dict:
    return {"schema_version": SCHEMA_VERSION, "domain": DOMAIN, "simulated": True, "kind": kind,
            "owner_id": store.owner_id, "org_id": store.org_id, "experiment_id": experiment_id,
            "period": period, "disclaimer": DISCLAIMER, **extra, "data": data}


def export_all(store: LedgerStore, out_dir: str | Path, *, include_abstentions: bool = False,
               allow_in_repo: bool = False, generated_at: str | None = None) -> dict:
    out_dir = Path(out_dir).expanduser().resolve()
    if not allow_in_repo and _repo_root(out_dir) is not None:
        raise ValueError("refusing to export inside a git working tree; choose a directory outside the repository")
    base = out_dir / API_PREFIX / DOMAIN / "orgs" / check_id(store.org_id, "org_id") / "users" / check_id(store.owner_id, "owner_id")
    tmp = base.with_name(base.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    files: dict[str, bytes] = {}

    def put(rel: str, doc: dict):
        files[rel] = (json.dumps(jsonable(doc), sort_keys=True, indent=1, allow_nan=False) + "\n").encode()

    experiments = []
    for eid in store.experiments():
        meta, periods = store.read_meta(eid), store.periods(eid)
        summary = {"experiment_id": eid, "strategy": meta["strategy"], "params": meta["params"],
                   "symbol": meta["symbol"], "interval": meta["interval"],
                   "configuration": meta["configuration"], "periods_available": periods,
                   "final_test_consumed": "final_test" in periods,
                   "metrics": {p: store.read_period(eid, p)["risk"]["metrics"] for p in periods}}
        put(f"experiments/{eid}/summary.json", _envelope("experiment", store, eid, None, summary))
        for period in periods:
            log = store.read_period(eid, period)
            decisions = log["decisions"]
            abstained = sum(1 for d in decisions if d["action"] == "ABSTAIN")
            if not include_abstentions:
                decisions = [d for d in decisions if d["action"] != "ABSTAIN"]
            equity, down = _downsample(log["equity"], MAX_EQUITY_POINTS)
            parts = {"trades": (log["trades"], {}), "positions": (log["positions"], {}),
                     "equity": (equity, {"downsampled": down, "points_total": len(log["equity"])}),
                     "decisions": (decisions, {"abstentions_omitted": 0 if include_abstentions else abstained,
                                               "decisions_total": len(log["decisions"])}),
                     "risk": (log["risk"], {})}
            for kind, (data, extra) in parts.items():
                put(f"experiments/{eid}/{period}/{kind}.json", _envelope(kind, store, eid, period, data, **extra))
        experiments.append({"experiment_id": eid, "strategy": meta["strategy"], "interval": meta["interval"],
                            "symbol": meta["symbol"], "periods": periods,
                            "validation_net_return_pct": meta["results"]["validation"]["net_return_pct"]})
    catalogue = []
    for n, c in sorted(STRATEGIES.items()):
        try:
            defaults = c().params()
        except TypeError:           # strategies defined purely by data (e.g. JSON specs) have no default parameters
            defaults = None
        catalogue.append({"name": n, "description": (c.__doc__ or "").strip().split("\n")[0], "default_params": defaults})
    put("strategies.json", _envelope("strategies", store, None, None, catalogue))
    put("training_runs.json", _envelope("training_runs", store, None, None, store.summaries()))
    listing = {rel: {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)} for rel, b in sorted(files.items())}
    content_sha = hashlib.sha256(json.dumps(listing, sort_keys=True).encode()).hexdigest()
    manifest = _envelope("manifest", store, None, None, {"experiments": experiments, "files": listing},
                         content_sha256=content_sha,
                         generated_at=generated_at or datetime.now(timezone.utc).isoformat())
    put("manifest.json", manifest)
    for rel, blob in files.items():
        target = tmp / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    shutil.rmtree(base, ignore_errors=True)
    base.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, base)
    return {"path": str(base), "files": len(files), "experiments": len(experiments), "content_sha256": content_sha,
            "schema_version": SCHEMA_VERSION}
