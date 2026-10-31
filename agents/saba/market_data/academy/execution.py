"""Execution model: costs and intrabar exit resolution. Pure functions, no state.

Assumptions (see ACADEMY.md):
* Orders decided on completed bar t are filled at the OPEN of bar t+1.
* Spread: full quoted spread in bps; buys pay mid+half, sells receive mid-half.
* Slippage: extra adverse bps on market orders and stop exits; NOT on take-profit
  limit exits (a limit never fills worse than its price).
* Fee: bps of notional on every fill.
* Gaps: a stop that the open has already crossed fills at the open (not the stop level).
* Ambiguity: OHLC does not reveal intrabar order. If one bar touches both stop and
  take-profit, the stop is assumed to fill first and the trade is flagged ambiguous.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CostModel:
    fee_bps: float = 2.0
    spread_bps: float = 3.0
    slippage_bps: float = 2.0

    def __post_init__(self):
        if min(self.fee_bps, self.spread_bps, self.slippage_bps) < 0:
            raise ValueError("costs cannot be negative")

    def fill_price(self, reference: float, trade_sign: int, *, slippage: bool = True) -> float:
        """trade_sign: +1 buy, -1 sell. Always moves the price against the trader."""
        adverse_bps = self.spread_bps / 2 + (self.slippage_bps if slippage else 0.0)
        return reference * (1 + trade_sign * adverse_bps / 10_000)

    def scaled(self, spread_multiplier: float = 1.0, slippage_multiplier: float = 1.0) -> "CostModel":
        return CostModel(self.fee_bps, self.spread_bps * spread_multiplier, self.slippage_bps * slippage_multiplier)

    def fee(self, notional: float) -> float:
        return abs(notional) * self.fee_bps / 10_000

    def as_dict(self) -> dict:
        return asdict(self)


def resolve_bar_exit(side: int, stop: float | None, take_profit: float | None,
                     o: float, h: float, l: float, liquidation: float | None = None):
    """Decide whether a protective exit triggers inside one bar.

    side: +1 long, -1 short. `liquidation` is the broker stop-out level (margin call); the adverse
    level that price reaches FIRST (the nearer one) wins, the stop-loss on ties.
    Returns None or (reason, reference_price, ambiguous).
    """
    levels = []
    if stop is not None:
        levels.append(("stop", stop))
    if liquidation is not None:
        levels.append(("liquidation", liquidation))
    # nearest adverse level is hit first: highest for longs, lowest for shorts
    adverse = None
    if levels:
        adverse = (max if side == 1 else min)(levels, key=lambda x: (x[1] * side, x[0] == "stop"))
    kind, level = adverse if adverse else (None, None)
    loss_reason = "stop_loss" if kind == "stop" else "liquidation"
    gap_reason = "stop_gap" if kind == "stop" else "liquidation_gap"
    if side == 1:
        if level is not None and o <= level:
            return gap_reason, o, False
        if take_profit is not None and o >= take_profit:
            return "take_profit_gap", o, False
        loss_hit = level is not None and l <= level
        tp_hit = take_profit is not None and h >= take_profit
    else:
        if level is not None and o >= level:
            return gap_reason, o, False
        if take_profit is not None and o <= take_profit:
            return "take_profit_gap", o, False
        loss_hit = level is not None and h >= level
        tp_hit = take_profit is not None and l <= take_profit
    if loss_hit and tp_hit:
        return loss_reason, level, True  # conservative: adverse level first
    if loss_hit:
        return loss_reason, level, False
    if tp_hit:
        return "take_profit", take_profit, False
    return None
