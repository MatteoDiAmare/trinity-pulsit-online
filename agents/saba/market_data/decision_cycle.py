"""Saba's first auditable research decision cycle. No broker or order access."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import hashlib
import json
import math
import sqlite3
from .intraday_policy import assess_late_session


@dataclass(frozen=True)
class Thesis:
    symbol: str
    as_of: datetime
    side: str  # LONG, SHORT, or ABSTAIN
    rationale: str
    invalidation_price: float | None
    expected_holding_minutes: float
    confidence: float
    evidence_id: str


def validate(thesis: Thesis) -> None:
    if thesis.as_of.tzinfo is None or thesis.as_of.utcoffset() is None:
        raise ValueError("aware timestamp required")
    if thesis.side not in {"LONG", "SHORT", "ABSTAIN"}:
        raise ValueError("invalid side")
    if not thesis.symbol or not thesis.evidence_id or not thesis.rationale.strip():
        raise ValueError("symbol, evidence and rationale required")
    if not math.isfinite(thesis.confidence) or not 0 <= thesis.confidence <= 1:
        raise ValueError("invalid confidence")
    if not math.isfinite(thesis.expected_holding_minutes) or thesis.expected_holding_minutes <= 0:
        raise ValueError("invalid holding time")
    if thesis.side != "ABSTAIN" and (thesis.invalidation_price is None or
            not math.isfinite(thesis.invalidation_price) or thesis.invalidation_price <= 0):
        raise ValueError("trade thesis requires finite positive invalidation price")


def evaluate(thesis: Thesis, *, exit_buffer_minutes: float = 5.0) -> dict:
    """A time-only research gate; never grants order permission."""
    validate(thesis)
    gate = assess_late_session(thesis.as_of, thesis.expected_holding_minutes, exit_buffer_minutes)
    accepted = thesis.side != "ABSTAIN" and gate.decision == "TIME_FEASIBLE"
    return {
        "thesis": {**asdict(thesis), "as_of": thesis.as_of.isoformat()},
        "decision": "RESEARCH_CANDIDATE" if accepted else "ABSTAIN",
        "time_gate": asdict(gate),
        "execution": "DISABLED",
        "unassessed": ["live bid/ask", "spread", "slippage", "margin", "economic events",
                       "portfolio exposure", "empirical edge"],
    }


class ExperienceJournal:
    """SQLite append-only event log with deterministic content hashes."""
    def __init__(self, path: str | Path):
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS experiences (
                id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL,
                evidence_id TEXT NOT NULL, payload TEXT NOT NULL,
                sha256 TEXT NOT NULL UNIQUE)""")

    def record(self, result: dict) -> str:
        if result.get("execution") != "DISABLED":
            raise ValueError("research-only records")
        payload = json.dumps(result, sort_keys=True, allow_nan=False)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR IGNORE INTO experiences(recorded_at,evidence_id,payload,sha256) VALUES(?,?,?,?)",
                (datetime.now().astimezone().isoformat(), result["thesis"]["evidence_id"], payload, digest))
        return digest

    def read(self) -> list[dict]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT payload FROM experiences ORDER BY id").fetchall()
        return [json.loads(row[0]) for row in rows]
