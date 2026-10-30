"""Conservative, explainable context interpretation for completed OHLC signals.

No execution, prediction, or trade recommendation. A 'supported' result only
means consistency with the supplied, point-in-time contextual evidence.
"""
from __future__ import annotations
import math
from typing import Any


def assess_context(
    *, direction: str, structure: str,
    at_known_support: bool | None = None,
    at_known_resistance: bool | None = None,
    volatility_ratio: float | None = None,
    spread_bps: float | None = None,
    max_spread_bps: float = 8.0,
    event_risk: bool | None = None,
    data_valid: bool = True,
) -> dict[str, Any]:
    """Return supported, contradicted or insufficient_evidence with reasons.

    Inputs must be computed using only information available at assessment
    time. No price prediction, position or order is returned.
    """
    if direction not in {"bullish", "bearish"}:
        raise ValueError("direction must be bullish or bearish")
    if structure not in {"bullish", "bearish", "mixed", "unknown"}:
        raise ValueError("invalid structure")
    if not math.isfinite(max_spread_bps) or max_spread_bps <= 0:
        raise ValueError("max_spread_bps must be positive and finite")
    for label, value in (("volatility_ratio", volatility_ratio), ("spread_bps", spread_bps)):
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError(f"{label} must be finite and nonnegative")

    reasons: list[str] = []
    missing: list[str] = []
    conflicts: list[str] = []
    if not data_valid:
        conflicts.append("invalid_or_stale_data")
    if event_risk is True:
        conflicts.append("scheduled_event_risk")
    elif event_risk is None:
        missing.append("event_calendar")
    if spread_bps is None:
        missing.append("spread")
    elif spread_bps > max_spread_bps:
        conflicts.append("spread_exceeds_research_limit")
    if structure == "unknown":
        missing.append("confirmed_structure")
    elif structure == "mixed":
        missing.append("directional_structure")
    elif structure != direction:
        conflicts.append("counter_to_confirmed_structure")
    else:
        reasons.append("aligned_with_confirmed_structure")

    if direction == "bullish":
        if at_known_resistance is True:
            conflicts.append("at_known_resistance")
        if at_known_support is True:
            reasons.append("at_known_support")
    else:
        if at_known_support is True:
            conflicts.append("at_known_support")
        if at_known_resistance is True:
            reasons.append("at_known_resistance")

    if volatility_ratio is None:
        missing.append("relative_volatility")
    elif volatility_ratio > 3.0:
        conflicts.append("unusually_large_range_vs_prior_atr")
    else:
        reasons.append("volatility_within_baseline_limit")

    if conflicts:
        status = "contradicted"
    elif missing or not reasons:
        status = "insufficient_evidence"
    else:
        status = "supported"
    return {
        "status": status,
        "supporting_evidence": reasons,
        "conflicts": conflicts,
        "missing_context": missing,
        "note": "Descriptive educational classification only; no forecast or order.",
    }
