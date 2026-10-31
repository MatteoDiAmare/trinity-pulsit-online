"""Replay simulator: plays bars chronologically, one position at a time.

Timeline per bar i (trading period):
  1. protective exits / gap checks for a carried position (uses bar i only)
  2. risk-governor flatten order from the previous close, then the pending order decided at
     bar i-1 fills at the open of bar i (after mandatory-stop and sizing checks)
  3. protective exits again on bar i for a position just opened at the open
  4. mark-to-market at close, record equity, update the risk governor
  5. strategy sees bars[0..i] (never later) and may queue an order for bar i+1
A remaining position is closed at the final bar's close ("end_of_data").

Risk: every entry needs a stop-loss; size comes from risk.size_position; a liquidation (stop-out)
level from the instrument's maintenance margin is simulated; stress can add slippage or make stops
fail. Any loss beyond the account balance is recorded as `shortfall` (money the operator would owe):
the design goal is that this is always zero, and tests/stress runs check it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import numpy as np
import pandas as pd

from .execution import CostModel, resolve_bar_exit
from .instruments import InstrumentSpec
from .risk import RiskProfile, StressModel, size_position
from .strategies import Decision, MarketView, PositionInfo, Strategy


@dataclass(frozen=True)
class SimConfig:
    capital: float = 1000.0
    costs: CostModel | None = None         # None -> instrument costs, else CostModel() defaults
    max_exposure: float | None = 1.0       # max notional / equity; None -> derived from profile/instrument
    min_notional: float = 1.0
    allow_short: bool = True
    instrument: InstrumentSpec | None = None
    risk: RiskProfile = field(default_factory=RiskProfile)
    stress: StressModel = field(default_factory=StressModel)
    seed: int = 0                          # drives stop-failure draws only

    def __post_init__(self):
        if self.capital <= 0 or self.min_notional < 0 or (self.max_exposure is not None and self.max_exposure <= 0):
            raise ValueError("capital and max_exposure must be positive")

    @property
    def effective_costs(self) -> CostModel:
        base = self.costs or (self.instrument.costs if self.instrument else CostModel())
        return base.scaled(self.stress.spread_multiplier, self.stress.slippage_multiplier)

    def leverage_cap(self) -> float:
        caps = [c for c in (self.max_exposure, self.risk.max_leverage,
                            self.instrument.max_leverage if self.instrument else None) if c is not None]
        return min(caps) if caps else 1.0

    def to_dict(self) -> dict:
        base_costs = self.costs or (self.instrument.costs if self.instrument else CostModel())
        return {"capital": self.capital, "costs": base_costs.as_dict(), "max_exposure": self.max_exposure,
                "min_notional": self.min_notional, "allow_short": self.allow_short, "seed": self.seed,
                "leverage_cap": self.leverage_cap(), "risk": self.risk.to_dict(), "stress": self.stress.to_dict(),
                "instrument": self.instrument.to_dict() if self.instrument else None}

    @staticmethod
    def from_dict(d: dict) -> "SimConfig":
        return SimConfig(d["capital"], CostModel(**d["costs"]), d["max_exposure"], d.get("min_notional", 1.0),
                         d["allow_short"],
                         InstrumentSpec.from_dict(d["instrument"]) if d.get("instrument") else None,
                         RiskProfile(**d["risk"]), StressModel(**d["stress"]), d.get("seed", 0))


@dataclass
class SimResult:
    decisions: list
    orders: list
    trades: list
    equity: pd.DataFrame
    config: SimConfig
    halted: str | None = None
    risk_events: list = field(default_factory=list)
    shortfall: float = 0.0


class Simulator:
    def __init__(self, config: SimConfig | None = None):
        self.cfg = config or SimConfig()

    def run(self, bars: pd.DataFrame, strategy: Strategy, *, trade_start: int = 0,
            trade_end: int | None = None) -> SimResult:
        """Trade bars[trade_start:trade_end]. Bars before trade_start are history only;
        bars from trade_end onward are never read."""
        end = len(bars) if trade_end is None else trade_end
        if not 0 <= trade_start < end <= len(bars):
            raise ValueError("invalid trading window")
        bars = bars.iloc[:end]
        arrays = {k: bars[c].to_numpy(dtype=float, copy=True) for k, c in
                  (("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close"), ("volume", "Volume"))}
        for a in arrays.values():
            a.setflags(write=False)
        index = bars.index
        cfg = self.cfg
        self._costs = cfg.effective_costs
        self._lev = cfg.leverage_cap()
        self._rng = np.random.default_rng(cfg.seed)
        self._pos = None
        self._balance = cfg.capital
        self._peak = cfg.capital
        self._day, self._day_start, self._last_eq = None, cfg.capital, cfg.capital
        self._day_halt = self._dd_halt = False
        self._streak = 0
        self._flatten = None
        self._events, self._shortfall = [], 0.0
        decisions, orders, trades, equity_rows = [], [], [], []
        pending: Decision | None = None
        halted = None
        o, h, l, c = arrays["open"], arrays["high"], arrays["low"], arrays["close"]
        prof = cfg.risk

        for i in range(trade_start, end):
            ts = index[i]
            day = ts.date()
            if day != self._day:           # new UTC day: daily limits reset
                self._day, self._day_start = day, self._last_eq
                if self._day_halt:
                    self._event(ts, "daily_halt_lifted")
                self._day_halt, self._streak = False, 0
            last = i == end - 1
            if halted is None:
                if self._pos is not None:
                    self._protective(i, ts, o[i], h[i], l[i], orders, trades, gap_only=True)
                if self._flatten is not None:
                    if self._pos is not None:
                        self._close(i, ts, o[i], self._flatten, False, orders, trades, market=True)
                    self._flatten = None
                if pending is not None:
                    self._fill_pending(i, ts, pending, o[i], orders, trades)
                    pending = None
                if self._pos is not None:
                    self._protective(i, ts, o[i], h[i], l[i], orders, trades, gap_only=False)
            if last and self._pos is not None:
                self._close(i, ts, c[i], "end_of_data", False, orders, trades, market=True)
            unreal = 0.0 if self._pos is None else self._pos["side"] * (c[i] - self._pos["entry"]) * self._pos["qty"]
            eq = self._balance + unreal
            if halted is None and eq <= 0:
                if self._pos is not None:
                    self._close(i, ts, c[i], "ruin", False, orders, trades, market=True)
                halted = "ruin: equity exhausted"
                self._event(ts, "ruin", shortfall=self._shortfall)
                eq = self._balance = max(self._balance, 0.0)
            self._last_eq = eq
            if halted is None:
                self._govern(ts, eq)
            equity_rows.append({"timestamp": ts, "balance": self._balance, "equity": eq,
                                "position_side": 0 if self._pos is None else self._pos["side"],
                                "quantity": 0.0 if self._pos is None else self._pos["qty"], "close": c[i]})
            if last or halted is not None:
                continue
            view = MarketView(arrays, index, i + 1)
            info = None
            if self._pos is not None:
                info = PositionInfo(self._pos["side"], self._pos["entry"], self._pos["qty"], i - self._pos["bar"])
            d = strategy.on_bar(view, info)
            decisions.append({"timestamp": ts, "action": d.action, "reason": d.reason,
                              "size": d.size, "stop_loss_pct": d.stop_loss_pct,
                              "take_profit_pct": d.take_profit_pct,
                              "position_side": 0 if info is None else info.side, "equity": eq})
            if d.action != "ABSTAIN":
                pending = d
        if halted is None and self._dd_halt:
            halted = "max_drawdown_halt"
        eq_df = pd.DataFrame(equity_rows).set_index("timestamp")
        return SimResult(decisions, orders, trades, eq_df, cfg, halted, self._events, self._shortfall)

    # ---- risk governor ---------------------------------------------------
    def _event(self, ts, kind, **detail):
        self._events.append({"timestamp": ts, "type": kind, **detail})

    def _govern(self, ts, eq):
        prof = self.cfg.risk
        self._peak = max(self._peak, eq)
        if not self._dd_halt and (eq / self._peak - 1) * 100 <= -prof.max_drawdown_halt_pct:
            self._dd_halt = True
            self._event(ts, "max_drawdown_halt", equity=eq, peak=self._peak)
            if self._pos is not None:
                self._flatten = "risk_flatten_drawdown"
        if not self._day_halt and self._day_start > 0 and (eq / self._day_start - 1) * 100 <= -prof.daily_loss_limit_pct:
            self._day_halt = True
            self._event(ts, "daily_loss_halt", equity=eq, day_start=self._day_start)
            if self._pos is not None:
                self._flatten = self._flatten or "risk_flatten_daily"

    def _entry_block(self):
        if self._dd_halt:
            return "risk_halt_drawdown"
        if self._day_halt:
            return "risk_halt_daily"
        if self._streak >= self.cfg.risk.max_consecutive_losses:
            return "risk_halt_loss_streak"
        return None

    # ---- orders ------------------------------------------------------------
    def _fill_pending(self, i, ts, d: Decision, open_price, orders, trades):
        cfg = self.cfg
        pos = self._pos
        if d.action == "CLOSE":
            if pos is not None:
                self._close(i, ts, open_price, "signal_close", False, orders, trades, market=True)
            return
        side = 1 if d.action == "BUY" else -1
        if side == -1 and not cfg.allow_short:
            return self._reject(ts, d, "short_selling_disabled", orders)
        if d.stop_loss_pct is None:
            return self._reject(ts, d, "stop_loss_required", orders)
        if pos is not None and pos["side"] == side:
            return self._reject(ts, d, "already_positioned", orders)
        blocked = self._entry_block()
        if blocked:
            return self._reject(ts, d, blocked, orders)
        if pos is not None:
            self._close(i, ts, open_price, "reversal", False, orders, trades, market=True)
        self._open(i, ts, side, d, open_price, orders)

    def _reject(self, ts, d, why, orders):
        orders.append({"timestamp": ts, "kind": "rejected", "action": d.action, "reason": why})

    def _open(self, i, ts, side, d, open_price, orders):
        cfg, costs = self.cfg, self._costs
        fill = costs.fill_price(open_price, side)
        sized, why = size_position(equity=self._balance, fill_price=fill, stop_pct=d.stop_loss_pct,
                                   size_fraction=d.size, leverage_cap=self._lev, fee_bps=costs.fee_bps,
                                   profile=cfg.risk, spec=cfg.instrument)
        if sized is None:
            return self._reject(ts, d, why, orders)
        qty = sized["quantity"]
        notional = qty * fill
        if notional < max(cfg.min_notional, 1e-12):
            return self._reject(ts, d, "insufficient_capital", orders)
        fee = costs.fee(notional)
        if cfg.instrument is not None and self._balance - fee - cfg.instrument.maintenance_margin_fraction * notional <= 0:
            return self._reject(ts, d, "insufficient_margin", orders)
        self._balance -= fee
        stop = fill * (1 - side * d.stop_loss_pct / 100)
        tp = None if d.take_profit_pct is None else fill * (1 + side * d.take_profit_pct / 100)
        liq = None
        if cfg.instrument is not None:
            maint = cfg.instrument.maintenance_margin_fraction * notional
            level = fill - side * (self._balance - maint) / qty
            liq = level if level > 0 else None
        self._pos = {"side": side, "qty": qty, "entry": fill, "entry_ref": open_price, "entry_ts": ts, "bar": i,
                     "stop": stop, "tp": tp, "liq": liq, "entry_fee": fee, "risk_amount": sized["risk_amount"],
                     "binding": sized["binding_limit"]}
        orders.append({"timestamp": ts, "kind": "entry", "action": d.action, "side": side, "quantity": qty,
                       "reference_price": open_price, "fill_price": fill, "fee": fee, "stop": stop,
                       "take_profit": tp, "liquidation_price": liq, "notional": notional,
                       "leverage_used": notional / max(self._balance, 1e-12), "risk_amount": sized["risk_amount"],
                       "worst_case_loss": sized["worst_case_loss"], "binding_limit": sized["binding_limit"],
                       "reason": d.reason})

    def _protective(self, i, ts, o, h, l, orders, trades, *, gap_only):
        p = self._pos
        if gap_only:   # carried position: only the open can already have crossed a level
            res = resolve_bar_exit(p["side"], p["stop"], p["tp"], o, o, o, p["liq"])
        else:
            res = resolve_bar_exit(p["side"], p["stop"], p["tp"], o, h, l, p["liq"])
        if res is None:
            return
        reason, ref, ambiguous = res
        stress = self.cfg.stress
        failed = False
        if reason in ("stop_loss", "liquidation") and stress.stop_failure_prob > 0 \
                and self._rng.random() < stress.stop_failure_prob:
            ref = l if p["side"] == 1 else h          # not honoured: fills at the bar's worst price
            reason, failed = "stop_failed", True
        adverse = stress.stop_slippage_pct + (stress.stop_failure_extra_pct if failed else 0.0)
        self._close(i, ts, ref, reason, ambiguous, orders, trades,
                    market=reason.startswith(("stop", "liquidation")), extra_adverse_pct=adverse)

    def _close(self, i, ts, ref, reason, ambiguous, orders, trades, *, market, extra_adverse_pct=0.0):
        costs, p = self._costs, self._pos
        side = p["side"]
        exit_fill = costs.fill_price(ref, -side, slippage=market)
        if extra_adverse_pct:
            exit_fill *= 1 - side * extra_adverse_pct / 100
        notional = p["qty"] * exit_fill
        fee = costs.fee(notional)
        gross = side * (exit_fill - p["entry"]) * p["qty"]
        self._balance += gross - fee
        if self._balance < 0:                       # loss beyond the account: would be operator debt
            self._shortfall += -self._balance
            self._event(ts, "negative_balance", shortfall=-self._balance, exit_reason=reason)
            self._balance = 0.0
        friction = p["qty"] * (abs(p["entry"] - p["entry_ref"]) + abs(exit_fill - ref))
        fees = p["entry_fee"] + fee
        net = gross - fees
        entry_notional = p["qty"] * p["entry"]
        self._streak = self._streak + 1 if net < 0 else 0
        orders.append({"timestamp": ts, "kind": "exit", "side": -side, "quantity": p["qty"],
                       "reference_price": ref, "fill_price": exit_fill, "fee": fee, "reason": reason,
                       "ambiguous": ambiguous})
        trades.append({"entry_ts": p["entry_ts"], "exit_ts": ts, "side": side, "quantity": p["qty"],
                       "entry_price": p["entry"], "exit_price": exit_fill, "stop_price": p["stop"],
                       "gross_pnl": gross, "fees": fees, "spread_slippage_cost": friction, "net_pnl": net,
                       "net_return_pct": net / entry_notional * 100, "risk_amount": p["risk_amount"],
                       "r_multiple": net / p["risk_amount"] if p["risk_amount"] > 0 else None,
                       "binding_limit": p["binding"], "bars_held": i - p["bar"],
                       "exit_reason": reason, "ambiguous": ambiguous})
        self._pos = None
