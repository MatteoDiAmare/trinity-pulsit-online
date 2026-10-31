"""Research integrity: protect the scientific value of the final test without manual approvals.

Mechanisms (all enforced in code, none depend on the agent's discipline):
* Test frontier  - a final test consumes its whole window. Later training runs may use those bars as
  development data, but their NEW final-test window must start after the last consumed bar. If not
  enough fresh bars exist, training refuses until more data has arrived.
* Window budget  - at most FINAL_TESTS_PER_WINDOW different experiments may be confirmed on the same
  window; an experiment can be final-tested only once (no override).
* Spec hash      - identical experiments (strategy, params, config, data, split, code version) are
  never re-run or re-counted; the stored result is reused.
* Trial counting - every distinct experiment on a market/interval is counted; the selection is
  compared with the Sharpe that pure luck would reach after that many trials.
* Code version   - git commit (and dirty flag) is recorded with every experiment.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import pandas as pd

FINAL_TESTS_PER_WINDOW = 3
MIN_FRESH_TEST_BARS = 20


@lru_cache(maxsize=1)
def code_version() -> str:
    env = os.environ.get("SABA_CODE_VERSION")
    if env:
        return env
    here = Path(__file__).resolve().parent
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=here, capture_output=True, text=True, timeout=10)
        if head.returncode != 0:
            return "unknown"
        dirty = subprocess.run(["git", "status", "--porcelain", "--", str(here.parent)], cwd=here,
                               capture_output=True, text=True, timeout=30).stdout.strip()
        return head.stdout.strip() + ("+dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def dataset_sha256(frame: pd.DataFrame) -> str:
    return hashlib.sha256(pd.util.hash_pandas_object(frame, index=True).values.tobytes()).hexdigest()


def spec_hash(strategy: str, params: dict, sim_config: dict, dev_sha256: str, split: dict, version: str) -> str:
    blob = json.dumps({"strategy": strategy, "params": params, "sim_config": sim_config, "dev": dev_sha256,
                       "split": split, "code": version}, sort_keys=True, allow_nan=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def market_key(symbol: str, interval: str) -> str:
    return f"{symbol}|{interval}"


def window_id(symbol: str, interval: str, test_start_utc: str) -> str:
    return hashlib.sha256(f"{symbol}|{interval}|{test_start_utc}".encode()).hexdigest()[:16]


class SpecIndex:
    """Side index inside the registry's own SQLite file (the registry table itself is untouched)."""

    def __init__(self, registry):
        self.db = registry.db
        self.db.execute("""CREATE TABLE IF NOT EXISTS academy_spec_index (
            spec_hash TEXT PRIMARY KEY, experiment_id INTEGER NOT NULL, market TEXT NOT NULL, created_at_utc TEXT NOT NULL)""")
        self.db.commit()

    def lookup(self, h: str):
        row = self.db.execute("SELECT experiment_id FROM academy_spec_index WHERE spec_hash=?", (h,)).fetchone()
        return None if row is None else int(row[0])

    def add(self, h: str, experiment_id: int, market: str):
        self.db.execute("INSERT OR IGNORE INTO academy_spec_index VALUES (?,?,?,?)",
                        (h, experiment_id, market, datetime.now(timezone.utc).isoformat()))
        self.db.commit()

    def trials(self, market: str) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM academy_spec_index WHERE market=?", (market,)).fetchone()[0])


def final_tests(registry, symbol: str, interval: str) -> list[dict]:
    """All consumed final tests of this market (config of each academy:final_test record)."""
    out = []
    for (cfg_json,) in registry.db.execute("SELECT configuration_json FROM experiments WHERE name='academy:final_test' ORDER BY id"):
        cfg = json.loads(cfg_json)
        if "symbol" not in cfg:     # records written before symbol/interval were stored
            row = registry.db.execute("SELECT configuration_json FROM experiments WHERE id=?", (cfg["experiment_id"],)).fetchone()
            base = json.loads(row[0]) if row else {}
            cfg = {**cfg, "symbol": base.get("symbol"), "interval": base.get("interval")}
        if cfg.get("symbol") == symbol and cfg.get("interval") == interval:
            out.append(cfg)
    return out


def test_frontier(registry, symbol: str, interval: str):
    """Timestamp of the last bar consumed by any final test of this market, or None."""
    ends = [pd.Timestamp(c["test_end_utc"]) for c in final_tests(registry, symbol, interval)]
    return max(ends) if ends else None


def expected_max_noise_sharpe(n_trials: int, n_bars: int, bars_per_year: float) -> float:
    """Annualized Sharpe that the best of n_trials zero-edge strategies would reach by luck (approx.)."""
    if n_bars < 2:
        return float("inf")
    return math.sqrt(2 * math.log(max(n_trials, 2))) / math.sqrt(n_bars) * math.sqrt(bars_per_year)
