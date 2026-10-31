"""Full per-experiment run logs (decisions, orders, trades, positions, equity, risk).

Stored beside the registry as gzip JSON:  <root>/academy_runs/<experiment_id>/{meta.json,<period>.json.gz}
Periods: train, validation, final_test. Everything here is SIMULATED; it never touches real
holdings or bookkeeping data. Each run carries owner_id/org_id so the model is multi-tenant ready.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd

PERIODS = ("train", "validation", "final_test")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEFAULT_OWNER, DEFAULT_ORG = "local", "personal"


def check_id(value: str, what: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError(f"invalid {what}: use 1-64 characters of A-Z a-z 0-9 _ -")
    return value


def jsonable(o):
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, pd.Timestamp):
        return o.isoformat()
    if isinstance(o, np.generic):
        o = o.item()
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return o


def result_payload(result, metrics: dict) -> dict:
    """Turn a SimResult into plain, complete log tables."""
    eq = result.equity
    start = result.config.capital
    equity = eq["equity"].to_numpy(dtype=float)
    peak = np.maximum.accumulate(np.concatenate([[start], equity]))[1:]
    dd = (equity / np.where(peak > 0, peak, 1) - 1) * 100
    equity_rows = [{"timestamp": ts, "balance": r.balance, "equity": r.equity, "drawdown_pct": d,
                    "position_side": int(r.position_side), "quantity": r.quantity, "close": r.close}
                   for (ts, r), d in zip(eq.iterrows(), dd)]
    positions = [{"opened": t["entry_ts"], "closed": t["exit_ts"], "side": t["side"], "quantity": t["quantity"],
                  "entry_price": t["entry_price"], "exit_price": t["exit_price"], "bars_held": t["bars_held"],
                  "exit_reason": t["exit_reason"], "net_pnl": t["net_pnl"]} for t in result.trades]
    trough = int(np.argmin(dd)) if len(dd) else 0
    risk = {"metrics": metrics,
            "max_drawdown_pct": metrics["max_drawdown_pct"],
            "drawdown_trough_timestamp": eq.index[trough] if len(dd) else None,
            "worst_trade_net_pnl": min((t["net_pnl"] for t in result.trades), default=None),
            "best_trade_net_pnl": max((t["net_pnl"] for t in result.trades), default=None)}
    return jsonable({"decisions": result.decisions, "orders": result.orders, "trades": result.trades,
                     "positions": positions, "equity": equity_rows, "risk": risk, "halted": result.halted})


def _atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class LedgerStore:
    def __init__(self, root: str | Path, owner_id: str = DEFAULT_OWNER, org_id: str = DEFAULT_ORG):
        self.root = Path(root).expanduser() / "academy_runs"
        self.owner_id, self.org_id = check_id(owner_id, "owner_id"), check_id(org_id, "org_id")

    def _dir(self, experiment_id: int) -> Path:
        return self.root / str(int(experiment_id))

    def write_meta(self, experiment_id: int, meta: dict):
        existing = self._dir(experiment_id) / "meta.json"
        if existing.is_file():
            old = json.loads(existing.read_text())
            if (old.get("owner_id"), old.get("org_id")) != (self.owner_id, self.org_id):
                raise ValueError("ledger for this experiment id belongs to another owner/org; refusing to overwrite")
        meta = {**meta, "experiment_id": int(experiment_id), "owner_id": self.owner_id,
                "org_id": self.org_id, "domain": "simulation"}
        _atomic_write(self._dir(experiment_id) / "meta.json",
                      json.dumps(jsonable(meta), sort_keys=True, allow_nan=False).encode())

    def write_period(self, experiment_id: int, period: str, payload: dict):
        if period not in PERIODS:
            raise ValueError(f"unknown period {period}")
        raw = json.dumps(jsonable(payload), sort_keys=True, allow_nan=False).encode()
        _atomic_write(self._dir(experiment_id) / f"{period}.json.gz", gzip.compress(raw, mtime=0))

    def read_meta(self, experiment_id: int) -> dict:
        return json.loads((self._dir(experiment_id) / "meta.json").read_text())

    def read_period(self, experiment_id: int, period: str) -> dict:
        return json.loads(gzip.decompress((self._dir(experiment_id) / f"{period}.json.gz").read_bytes()))

    def periods(self, experiment_id: int) -> list[str]:
        return [p for p in PERIODS if (self._dir(experiment_id) / f"{p}.json.gz").is_file()]

    def experiments(self) -> list[int]:
        """Experiment ids belonging to THIS owner/org only."""
        out = []
        for d in sorted(self.root.glob("*/meta.json"), key=lambda p: int(p.parent.name)):
            m = json.loads(d.read_text())
            if m.get("owner_id") == self.owner_id and m.get("org_id") == self.org_id:
                out.append(int(d.parent.name))
        return out

    def write_summary(self, summary_id: int, summary: dict):
        _atomic_write(self.root / "summaries" / f"{int(summary_id)}.json",
                      json.dumps(jsonable({**summary, "owner_id": self.owner_id, "org_id": self.org_id,
                                           "domain": "simulation"}), sort_keys=True, allow_nan=False).encode())

    def summaries(self) -> list[dict]:
        out = []
        for p in sorted((self.root / "summaries").glob("*.json"), key=lambda p: int(p.stem)):
            s = json.loads(p.read_text())
            if s.get("owner_id") == self.owner_id and s.get("org_id") == self.org_id:
                out.append({"summary_id": int(p.stem), **s})
        return out
