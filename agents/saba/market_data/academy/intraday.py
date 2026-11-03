"""Intraday trading model: Saba's intraday policy inside the simulator, and credible intraday costs.

Everything here is OPTIONAL and off by default, so daily studies (and every earlier result) are byte-for-byte unchanged.

1. `SessionRules` - Saba's mandatory intraday-only policy (`market_data/intraday_policy.py`: no new entries from 21:30, flat by 21:45 Europe/Stockholm)
   applied to every bar of a simulation. A position is never carried across the cut-off: it is closed at the OPEN of the first bar that would end after
   the flatten time (so with coarse bars it is closed slightly EARLY, never late), and no entry is allowed in a bar that is not completely inside the
   allowed window. The daily loss limit resets at local midnight in the same timezone. Holding positions overnight is only possible when a study says
   `allow_overnight: true`, which is recorded in the study spec.

2. `IntradayCosts` - what a flat bps cost cannot express:
   * spread varies over the day (thin at the daily rollover and in the Asian night, tight in the US/Europe overlap) and at weekends;
   * slippage grows with how violent the market is: a base amount plus a fraction of the (previous) bar's range;
   * financing/swap for a leveraged position that is carried across the daily rollover (long and short can differ, Wednesday may count triple).
   All numbers in `data/intraday_profiles.json` are conservative research ASSUMPTIONS, not measured broker terms. The study therefore also supports a cost
   multiplier (double costs) as a robustness test.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd

PROFILE_PATH = Path(__file__).resolve().parent / "data" / "intraday_profiles.json"
DEFAULT_TZ = "Europe/Stockholm"


@dataclass(frozen=True)
class SessionRules:
    bar_minutes: int
    tz: str = DEFAULT_TZ
    last_entry_min: int = 21 * 60 + 30
    flatten_min: int = 21 * 60 + 45

    def __post_init__(self):
        if self.bar_minutes <= 0 or not 0 < self.last_entry_min < self.flatten_min <= 24 * 60:
            raise ValueError("invalid session rules: need 0 < last_entry < flatten <= 24:00 and a positive bar length")

    @staticmethod
    def from_policy(bar_minutes: int, policy=None, tz: str = DEFAULT_TZ) -> "SessionRules":
        """Take the cut-offs from Saba's IntradayPolicy so the simulation and the policy cannot drift apart."""
        from ..intraday_policy import IntradayPolicy
        p = policy or IntradayPolicy()
        t = lambda x: x.hour * 60 + x.minute  # noqa: E731
        return SessionRules(bar_minutes, tz, t(p.last_entry), t(p.flatten_by))

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "SessionRules":
        return SessionRules(**d)

    def arrays(self, index: pd.DatetimeIndex) -> dict:
        """Per bar: entry_ok (an entry at the bar's open is allowed), must_flat (a carried position must be closed at the bar's open),
        day (local calendar-day key for the daily risk limits)."""
        local = index.tz_convert(self.tz)
        tod = (local.hour * 60 + local.minute).to_numpy()
        end = tod + self.bar_minutes
        entry_ok = (tod < self.last_entry_min) & (end <= self.flatten_min)
        must_flat = end > self.flatten_min
        # a bar that starts before the flatten time but ends after midnight is still "evening": covered because end > flatten_min
        day = local.normalize().tz_localize(None).to_numpy().astype("datetime64[D]").astype("int64")
        # A position must also be closed at the CLOSE of a bar when the next bar is already past the cut-off or on a later local day. This is what stops a
        # limited-hours market (no bar between the cut-off and tomorrow's open) from carrying a position overnight: closing "at the next open" would be tomorrow.
        flat_at_close = np.zeros(len(index), dtype=bool)
        if len(index) > 1:
            flat_at_close[:-1] = (day[1:] != day[:-1]) | must_flat[1:]
        return {"entry_ok": entry_ok, "must_flat": must_flat, "day": day, "flat_at_close": flat_at_close}


@dataclass(frozen=True)
class IntradayCosts:
    spread_hour_mult: tuple = (1.0,) * 24      # multiplier of the instrument's spread by UTC hour of the bar
    weekend_spread_mult: float = 1.0           # extra multiplier on Saturday/Sunday (UTC)
    slippage_range_fraction: float = 0.0       # slippage += fraction x range(bar) in bps
    financing_long_bps_per_day: float = 0.0    # charged on notional per rollover crossed (positive = cost)
    financing_short_bps_per_day: float = 0.0
    rollover_hour_utc: int | None = None       # None = no financing
    triple_weekday_utc: int | None = None      # weekday (Mon=0) whose rollover counts three days
    skip_weekend_rollover: bool = True         # no rollover charge on Saturday/Sunday (markets closed); False for 24/7 markets
    source: str = "assumption"

    def __post_init__(self):
        if len(self.spread_hour_mult) != 24 or min(self.spread_hour_mult) <= 0 or self.weekend_spread_mult <= 0 or self.slippage_range_fraction < 0:
            raise ValueError("invalid intraday costs: 24 positive hourly multipliers, positive weekend multiplier, non-negative slippage fraction")
        if self.rollover_hour_utc is not None and not 0 <= self.rollover_hour_utc < 24:
            raise ValueError("rollover_hour_utc must be 0..23")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["spread_hour_mult"] = list(self.spread_hour_mult)
        return d

    @staticmethod
    def from_dict(d: dict) -> "IntradayCosts":
        d = dict(d)
        d["spread_hour_mult"] = tuple(d.get("spread_hour_mult", (1.0,) * 24))
        return IntradayCosts(**d)

    def arrays(self, index: pd.DatetimeIndex) -> dict:
        """spread_mult per bar, and fin_days per bar = rollover days charged when carrying a position INTO this bar from the previous one."""
        utc = index.tz_convert("UTC")
        hours = utc.hour.to_numpy()
        wd = utc.dayofweek.to_numpy()
        mult = np.array(self.spread_hour_mult)[hours] * np.where(wd >= 5, self.weekend_spread_mult, 1.0)
        fin = np.zeros(len(index))
        if self.rollover_hour_utc is not None and len(index) > 1:
            days = utc.normalize().tz_localize(None)
            all_days = pd.date_range(days[0] - pd.Timedelta(days=1), days[-1], freq="D")
            w = np.ones(len(all_days))
            dwd = all_days.dayofweek.to_numpy()
            if self.skip_weekend_rollover:
                w[dwd >= 5] = 0.0
            if self.triple_weekday_utc is not None:
                w[dwd == self.triple_weekday_utc] = 3.0
            cum_before = np.concatenate([[0.0], np.cumsum(w)])          # sum of weights of days strictly before day k
            k = np.asarray((days - all_days[0]).days)
            after = (hours * 60 + utc.minute.to_numpy()) >= self.rollover_hour_utc * 60
            cum = cum_before[k] + np.where(after, w[k], 0.0)           # weights of all rollover instants at or before each bar start
            fin[1:] = np.diff(cum)
        return {"spread_mult": mult, "fin_days": fin}


# ---------------------------------------------------------------- profiles (data, not code)
def load_intraday_profiles() -> dict:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))["profiles"] if PROFILE_PATH.is_file() else {}


def session_kind(instrument_id: str) -> str | None:
    p = load_intraday_profiles().get(instrument_id)
    return p.get("session") if p else None


def intraday_costs(instrument_id: str) -> IntradayCosts | None:
    p = load_intraday_profiles().get(instrument_id)
    return IntradayCosts.from_dict(p["costs"]) if p and p.get("costs") else None
