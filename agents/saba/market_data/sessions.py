"""Timezone-aware session research features. Read-only; no trade signals."""
from __future__ import annotations
from datetime import time
from zoneinfo import ZoneInfo
import pandas as pd

LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")


def session_labels(index: pd.DatetimeIndex) -> pd.DataFrame:
    """Labels UTC-aware completed bar timestamps by local clock.

    London open is a *research anchor* for bullion, not an exchange open.
    """
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise ValueError("timezone-aware timestamps required")
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise ValueError("timestamps must be sorted and unique")
    london = index.tz_convert(LONDON)
    ny = index.tz_convert(NEW_YORK)
    return pd.DataFrame({
        "london_date": [x.date().isoformat() for x in london],
        "london_local": [x.isoformat() for x in london],
        "ny_local": [x.isoformat() for x in ny],
        "london_open_window": [x.weekday() < 5 and time(8, 0) <= x.time() < time(9, 0) for x in london],
        "ny_cash_open_window": [x.weekday() < 5 and time(9, 30) <= x.time() < time(10, 30) for x in ny],
        "asia_reference_window": [x.weekday() < 5 and time(0, 0) <= x.time() < time(8, 0) for x in london],
    }, index=index)


def prior_asia_range(bars: pd.DataFrame) -> pd.DataFrame:
    """Asia range known at London 08:00, only if 00:00–07:59 is complete.

    Assumes 1m bars timestamped at minute START (as in Yahoo archive).
    This is a research-defined window; weekends/holidays have no synthetic data.
    """
    if not {"High", "Low"}.issubset(bars.columns):
        raise ValueError("High/Low required")
    labels = session_labels(bars.index)
    london = bars.index.tz_convert(LONDON)
    result = pd.DataFrame(index=bars.index, columns=["asia_high", "asia_low"], dtype=float)
    for date in labels["london_date"].unique():
        loc = labels["london_date"] == date
        reference = loc & labels["asia_reference_window"]
        # 480 distinct minute bars required to prevent partial-session lookahead.
        if reference.sum() != 480:
            continue
        stamps = london[reference]
        if stamps[0].time() != time(0, 0) or stamps[-1].time() != time(7, 59):
            continue
        known = loc & (london.hour >= 8)
        result.loc[known, "asia_high"] = float(bars.loc[reference, "High"].max())
        result.loc[known, "asia_low"] = float(bars.loc[reference, "Low"].min())
    return result
