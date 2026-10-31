"""Risk management: mandatory stop-loss, risk-based sizing, leverage/margin caps and governor limits.

Design rule: the stop-loss is MANDATORY. There is deliberately no switch to turn that off. An entry
without a stop is rejected by the execution layer, whatever the strategy asks for.

Sizing takes the smallest of three limits:
  risk        loss if the stop fills AT its level  <= risk_per_trade_pct of equity
  leverage    notional <= equity x leverage cap
  gap_stress  loss if price gaps past the stop by the instrument's worst plausible gap
              (>= the stop distance) <= max_worst_case_loss_pct of equity
The third limit is what keeps one bad gap from taking more than a bounded share of the account,
which is how the operator is never put into debt by a market move the stop could not catch.
These are simulation rules. A real account additionally needs the broker's negative-balance
protection and terms verified, and an enforcement point outside the agent.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RiskProfile:
    risk_per_trade_pct: float = 1.0
    max_leverage: float | None = None
    max_worst_case_loss_pct: float = 5.0
    daily_loss_limit_pct: float = 3.0
    max_drawdown_halt_pct: float = 20.0
    max_consecutive_losses: int = 10

    def __post_init__(self):
        for name in ("risk_per_trade_pct", "max_worst_case_loss_pct", "daily_loss_limit_pct", "max_drawdown_halt_pct"):
            if not 0 < getattr(self, name) <= 100:
                raise ValueError(f"{name} must be in (0, 100]")
        if self.max_leverage is not None and self.max_leverage <= 0:
            raise ValueError("max_leverage must be positive")
        if self.max_consecutive_losses < 1:
            raise ValueError("max_consecutive_losses must be >= 1")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class StressModel:
    """Adverse-execution scenario. Defaults = no extra stress."""
    spread_multiplier: float = 1.0
    slippage_multiplier: float = 1.0
    stop_slippage_pct: float = 0.0       # extra adverse move on every stop / liquidation fill
    stop_failure_prob: float = 0.0       # stop is not honoured: fills at the bar's worst price
    stop_failure_extra_pct: float = 0.0  # additional adverse move when a stop fails

    def __post_init__(self):
        if min(self.spread_multiplier, self.slippage_multiplier) < 0 or self.stop_slippage_pct < 0 \
                or self.stop_failure_extra_pct < 0 or not 0 <= self.stop_failure_prob <= 1:
            raise ValueError("invalid stress parameters")

    def to_dict(self) -> dict:
        return asdict(self)


def size_position(*, equity: float, fill_price: float, stop_pct, size_fraction: float, leverage_cap: float,
                  fee_bps: float, profile: RiskProfile, spec=None):
    """Return (details, None) or (None, rejection_reason)."""
    if stop_pct is None:
        return None, "stop_loss_required"
    if spec is not None and stop_pct < spec.min_stop_pct:
        return None, "stop_too_tight"
    if equity <= 0 or fill_price <= 0:
        return None, "insufficient_capital"
    stop_frac = stop_pct / 100
    gap_frac = max(stop_frac, (spec.gap_stress_pct / 100) if spec is not None else 0.0)
    limits = {
        "risk": equity * profile.risk_per_trade_pct / 100 / (fill_price * stop_frac),
        "leverage": equity * leverage_cap / (fill_price * (1 + fee_bps / 10_000)),
        "gap_stress": equity * profile.max_worst_case_loss_pct / 100 / (fill_price * gap_frac),
    }
    binding = min(limits, key=limits.get)
    qty = limits[binding] * size_fraction
    if spec is not None:
        qty = math.floor(qty / spec.quantity_step + 1e-9) * spec.quantity_step
        if qty < spec.min_quantity:
            return None, "below_min_quantity"
    if qty <= 0:
        return None, "insufficient_capital"
    return {"quantity": qty, "binding_limit": binding, "limits": limits,
            "risk_amount": qty * fill_price * stop_frac,
            "worst_case_loss": qty * fill_price * gap_frac}, None
