"""Read-only, point-in-time Market Intelligence Engine for Saba.

Uses archived completed bars; no brokerage integration, orders or forecasts.
"""
from __future__ import annotations

import json
from pathlib import Path
import pandas as pd
from .archive import MarketArchive
from .candlesticks import describe_patterns
from .market_structure import structure_state, prior_range_breaks
from .volatility_volume import volatility_volume_features
from .market_context import assess_context


def resample_completed(bars: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate UTC minute bars into right-labelled, completed N-minute bars.

    Only fully populated buckets are retained; no synthetic candles over gaps.
    A timestamp labels the minute's *start*, so 09:00..09:04 becomes a bar
    labelled 09:05, available only after 09:05.
    """
    if minutes < 1:
        raise ValueError("minutes must be positive")
    if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.tz is None:
        raise ValueError("UTC-aware datetime index required")
    if not bars.index.is_monotonic_increasing or bars.index.has_duplicates:
        raise ValueError("timestamps must be sorted and unique")
    if minutes == 1:
        return bars.copy().set_axis(bars.index + pd.Timedelta(minutes=1))
    rule = f"{minutes}min"
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if "Volume" in bars.columns:
        agg["Volume"] = "sum"
    grouped = bars.resample(rule, label="right", closed="left", origin="start_day")
    result = grouped.agg(agg)
    counts = grouped["Close"].count()
    result = result.loc[counts == minutes].dropna(subset=["Open", "High", "Low", "Close"])
    return result


def analyze_frame(bars: pd.DataFrame, *, window: int = 20) -> dict:
    if len(bars) < max(window + 3, 30):
        raise ValueError("not enough completed bars")
    patterns = describe_patterns(bars)
    structure = structure_state(bars)
    breaks = prior_range_breaks(bars, lookback=window)
    volatility = volatility_volume_features(bars, window=window)
    last = bars.index[-1]
    last_patterns = [name for name, value in patterns.iloc[-1].items() if bool(value)]
    state = str(structure.iloc[-1]["structure"])
    vol = volatility.iloc[-1]
    def finite(value):
        return float(value) if pd.notna(value) else None
    # We deliberately do not invent bid/ask spread or event-calendar data.
    # The archived OHLC archive cannot establish either.
    directions = [x for x in ("bullish", "bearish") if x in last_patterns]
    assessments = {
        direction: assess_context(
            direction=direction, structure=state,
            volatility_ratio=finite(vol["range_to_prior_atr"]),
            spread_bps=None, event_risk=None,
        ) for direction in directions
    }
    return {
        "completed_at_utc": last.isoformat(),
        "bars": len(bars),
        "structure": state,
        "last_close": float(bars["Close"].iloc[-1]),
        "last_patterns": last_patterns,
        "range_to_prior_atr": finite(vol["range_to_prior_atr"]),
        "relative_volume": finite(vol["relative_volume"]) if "relative_volume" in vol else None,
        "close_above_prior_range": bool(breaks.iloc[-1]["close_above_prior_range"]),
        "close_below_prior_range": bool(breaks.iloc[-1]["close_below_prior_range"]),
        "assessments": assessments,
        "uncertainties": [
            "No verified bid/ask spread or executable depth in archived OHLC",
            "No point-in-time economic event calendar",
            "Yahoo futures volume provenance/contract roll must be verified before volume-based claims",
            "Context consistency is not a forecast or demonstrated trading edge",
        ],
    }


def analyze_archive(symbol: str = "GC=F", *, root: str | Path | None = None,
                    window: int = 20) -> dict:
    archive = MarketArchive(root)
    bars = archive.read(symbol, "1m")
    if bars.empty:
        raise ValueError("no archived minute bars")
    result = {"symbol": symbol, "source_interval": "1m",
              "raw_bars": len(bars), "timeframes": {},
              "hypotheses": [
                  {"question": "Does pattern + confirmed structure improve forward outcomes versus pattern alone?",
                   "test": "Chronological walk-forward with matched session controls and transaction costs",
                   "falsifier": "No incremental out-of-sample performance"},
                  {"question": "Does prior-normalized range expansion predict persistence after costs?",
                   "test": "Compare range-expansion buckets to same-session null entries",
                   "falsifier": "Effect vanishes on untouched periods or under plausible costs"},
              ],
              "execution": "disabled — research only"}
    for minutes in (1, 5, 15):
        frame = resample_completed(bars, minutes)
        try:
            result["timeframes"][f"{minutes}m"] = analyze_frame(frame, window=window)
        except ValueError as error:
            result["timeframes"][f"{minutes}m"] = {"error": str(error)}
    return result


def report_json(symbol: str = "GC=F", *, root: str | Path | None = None) -> str:
    return json.dumps(analyze_archive(symbol, root=root), indent=2, ensure_ascii=False)
