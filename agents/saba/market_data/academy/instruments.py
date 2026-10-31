"""Instrument model: everything the simulator must know about a market, as data (not code).

Specs live as versioned JSON in `instruments/` (git). ALL numbers are conservative research
assumptions, NOT verified broker terms: leverage caps, margin and costs must be verified against the
actual broker contract before any real trading is ever enabled (Academy has no broker connection).
A spec is stored in full inside every experiment's configuration, so old results stay reproducible
even if the JSON file is later edited.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .execution import CostModel

SPEC_DIR = Path(__file__).resolve().parent / "instruments"
MINUTES = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1h": 60, "90m": 90}


@dataclass(frozen=True)
class InstrumentSpec:
    id: str
    symbol: str                     # data-provider (Yahoo) symbol
    name: str
    asset_class: str
    quote_currency: str
    tick_size: float
    min_quantity: float
    quantity_step: float
    costs: CostModel
    max_leverage: float             # Saba's own hard cap (never above the broker's)
    broker_max_leverage_assumed: float | None   # informational, UNVERIFIED
    maintenance_margin_fraction: float          # stop-out when equity < this x notional
    gap_stress_pct: float           # worst plausible gap used to size positions (percent)
    min_stop_pct: float             # tighter stops than this are inside the noise/spread
    trading_hours_per_day: float
    trading_days_per_year: int
    timezone: str
    sessions: str
    notes: str = ""

    def __post_init__(self):
        if not self.id or not self.symbol:
            raise ValueError("instrument id and symbol required")
        for name in ("tick_size", "min_quantity", "quantity_step", "max_leverage", "gap_stress_pct",
                     "min_stop_pct", "trading_hours_per_day", "trading_days_per_year"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be positive")
        if not 0 <= self.maintenance_margin_fraction < 1:
            raise ValueError("maintenance_margin_fraction must be in [0, 1)")
        if self.broker_max_leverage_assumed is not None and self.max_leverage > self.broker_max_leverage_assumed:
            raise ValueError("Saba's leverage cap may not exceed the assumed broker cap")

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "InstrumentSpec":
        d = dict(d)
        d["costs"] = CostModel(**d["costs"])
        return InstrumentSpec(**d)

    @property
    def spec_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()

    def bars_per_year(self, interval: str) -> float:
        if interval in MINUTES:
            return self.trading_days_per_year * self.trading_hours_per_day * 60 / MINUTES[interval]
        return {"1d": float(self.trading_days_per_year), "5d": self.trading_days_per_year / 5,
                "1wk": 52.0, "1mo": 12.0, "3mo": 4.0}.get(interval, float(self.trading_days_per_year))


def available_instruments() -> dict[str, InstrumentSpec]:
    out = {}
    for path in sorted(SPEC_DIR.glob("*.json")):
        spec = InstrumentSpec.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if spec.id in out:
            raise ValueError(f"duplicate instrument id {spec.id}")
        out[spec.id] = spec
    return out


def load_instrument(id_or_symbol: str) -> InstrumentSpec:
    specs = available_instruments()
    if id_or_symbol in specs:
        return specs[id_or_symbol]
    for spec in specs.values():
        if spec.symbol == id_or_symbol:
            return spec
    raise ValueError(f"unknown instrument {id_or_symbol!r}; available: {sorted(specs)}")
