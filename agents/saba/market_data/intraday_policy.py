"""Mandatory intraday-only policy for Saba's simulated trading.

Uses Europe/Stockholm local time and aware datetimes. The cut-off is a
configurable training default, NOT a broker's market close. Enforcement must
also occur in the independent order gateway, including after crashes.
"""
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

STOCKHOLM = ZoneInfo("Europe/Stockholm")


@dataclass(frozen=True)
class IntradayPolicy:
    last_entry: time = time(21, 30)
    flatten_by: time = time(21, 45)

    def __post_init__(self):
        if not self.last_entry < self.flatten_by:
            raise ValueError("last_entry must precede flatten_by")

    def decision(self, now: datetime) -> str:
        """Return ENTRY_ALLOWED, CLOSE_ONLY, or FLATTEN_NOW."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("timezone-aware datetime required")
        local = now.astimezone(STOCKHOLM)
        if local.time() >= self.flatten_by:
            return "FLATTEN_NOW"
        if local.time() >= self.last_entry:
            return "CLOSE_ONLY"
        return "ENTRY_ALLOWED"

    def allow_new_position(self, now: datetime) -> bool:
        return self.decision(now) == "ENTRY_ALLOWED"


@dataclass(frozen=True)
class LateSessionAssessment:
    """Research gate; never grants permission beyond the hard policy."""
    decision: str
    minutes_remaining: float
    estimated_exit_minutes: float
    rationale: str


def assess_late_session(
    now: datetime,
    expected_holding_minutes: float,
    exit_buffer_minutes: float = 5.0,
    policy: IntradayPolicy | None = None,
) -> LateSessionAssessment:
    """Evaluate time-to-exit without inventing an edge or a risk multiplier.

    Expected holding duration must come from prior out-of-sample observations,
    not a desired profit target. A gateway must independently enforce flatten.
    """
    from datetime import timedelta
    import math
    if not math.isfinite(expected_holding_minutes) or expected_holding_minutes <= 0:
        raise ValueError("positive finite holding duration required")
    if not math.isfinite(exit_buffer_minutes) or exit_buffer_minutes < 0:
        raise ValueError("nonnegative finite exit buffer required")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    policy = policy or IntradayPolicy()
    local = now.astimezone(STOCKHOLM)
    cutoff = datetime.combine(local.date(), policy.flatten_by, tzinfo=STOCKHOLM)
    remaining = (cutoff - local).total_seconds() / 60
    required = expected_holding_minutes + exit_buffer_minutes
    gate = policy.decision(now)
    if gate == "FLATTEN_NOW":
        decision, reason = "FLATTEN_NOW", "Hard daily flatten deadline reached"
    elif gate == "CLOSE_ONLY":
        decision, reason = "CLOSE_ONLY", "New-entry deadline reached"
    elif required > remaining:
        decision, reason = "REJECT_ENTRY", "Insufficient time for expected holding and exit buffer"
    else:
        decision, reason = "TIME_FEASIBLE", "Time gate passed; spread, volatility, margin and risk remain unassessed"
    return LateSessionAssessment(decision, remaining, required, reason)
