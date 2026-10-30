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
