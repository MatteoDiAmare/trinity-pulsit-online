"""Replay simulator: plays bars chronologically, one position at a time.

Timeline per bar i (trading period):
  1. protective exits / gap checks for an open position (uses bar i only)
  2. pending order decided at bar i-1 fills at open of bar i
  3. protective exits again on the same bar for a position just opened at the open
  4. mark-to-market at close, record equity
  5. strategy sees bars[0..i] (never later) and may queue an order for bar i+1
Remaining position is closed at the final bar's close ("end_of_data").
"""
from __future__ import annotations

from dataclasses import dataclass, field
import numpy as np
import pandas as pd

from .execution import CostModel, resolve_bar_exit
from .strategies import Decision, MarketView, PositionInfo, Strategy


@dataclass(frozen=True)
class SimConfig:
    capital: float = 1000.0
    costs: CostModel = field(default_factory=CostModel)
    max_exposure: float = 1.0      # max notional as a multiple of current equity (1.0 = no leverage)
    min_notional: float = 1.0      # orders smaller than this are rejected
    allow_short: bool = True

    def __post_init__(self):
        if self.capital <= 0 or self.max_exposure <= 0 or self.min_notional < 0:
            raise ValueError("capital and max_exposure must be positive")


@dataclass
class SimResult:
    decisions: list
    orders: list
    trades: list
    equity: pd.DataFrame
    config: SimConfig
    halted: str | None = None


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
        cfg, costs = self.cfg, self.cfg.costs
        self._pos = None
        self._balance = cfg.capital
        decisions, orders, trades, equity_rows = [], [], [], []
        pending: Decision | None = None
        halted = None
        o, h, l, c = arrays["open"], arrays["high"], arrays["low"], arrays["close"]

        for i in range(trade_start, end):
            ts = index[i]
            last = i == end - 1
            if halted is None:
                if self._pos is not None:
                    self._protective(i, ts, o[i], h[i], l[i], orders, trades, check_gap_only=True)
                if pending is not None:
                    self._fill_pending(i, ts, pending, o[i], orders, trades)
                    pending = None
                    if self._pos is not None:
                        self._protective(i, ts, o[i], h[i], l[i], orders, trades, check_gap_only=False)
                elif self._pos is not None:
                    self._protective(i, ts, o[i], h[i], l[i], orders, trades, check_gap_only=False)
            if last and self._pos is not None:
                self._close(i, ts, c[i], "end_of_data", False, orders, trades, market=True)
            unreal = 0.0 if self._pos is None else self._pos["side"] * (c[i] - self._pos["entry"]) * self._pos["qty"]
            eq = self._balance + unreal
            if halted is None and eq <= 0:
                if self._pos is not None:
                    self._close(i, ts, c[i], "ruin", False, orders, trades, market=True)
                halted = "ruin: equity exhausted"
                eq = self._balance = max(self._balance, 0.0)
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
            actionable = d.action != "ABSTAIN"
            decisions.append({"timestamp": ts, "action": d.action, "reason": d.reason,
                              "size": d.size, "stop_loss_pct": d.stop_loss_pct,
                              "take_profit_pct": d.take_profit_pct,
                              "position_side": 0 if info is None else info.side, "equity": eq})
            if actionable:
                pending = d
        eq_df = pd.DataFrame(equity_rows).set_index("timestamp")
        return SimResult(decisions, orders, trades, eq_df, cfg, halted)

    # ---- internals -------------------------------------------------------
    def _fill_pending(self, i, ts, d: Decision, open_price, orders, trades):
        cfg = self.cfg
        pos = self._pos
        if d.action == "CLOSE":
            if pos is not None:
                self._close(i, ts, open_price, "signal_close", False, orders, trades, market=True)
            return
        side = 1 if d.action == "BUY" else -1
        if side == -1 and not cfg.allow_short:
            self._reject(ts, d, "short_selling_disabled", orders)
            return
        if pos is not None:
            if pos["side"] == side:
                self._reject(ts, d, "already_positioned", orders)
                return
            self._close(i, ts, open_price, "reversal", False, orders, trades, market=True)
        self._open(i, ts, side, d, open_price, orders)

    def _reject(self, ts, d, why, orders):
        orders.append({"timestamp": ts, "kind": "rejected", "action": d.action, "reason": why})

    def _open(self, i, ts, side, d, open_price, orders):
        cfg, costs = self.cfg, self.cfg.costs
        equity = self._balance
        budget = equity * cfg.max_exposure * d.size
        fill = costs.fill_price(open_price, side)
        qty = budget / (fill * (1 + costs.fee_bps / 10_000)) if budget > 0 else 0.0
        notional = qty * fill
        if equity <= 0 or notional < max(cfg.min_notional, 1e-12):
            self._reject(ts, d, "insufficient_capital", orders)
            return
        fee = costs.fee(notional)
        self._balance -= fee
        stop = tp = None
        if d.stop_loss_pct is not None:
            stop = fill * (1 - side * d.stop_loss_pct / 100)
        if d.take_profit_pct is not None:
            tp = fill * (1 + side * d.take_profit_pct / 100)
        self._pos = {"side": side, "qty": qty, "entry": fill, "entry_ref": open_price, "entry_ts": ts,
                     "bar": i, "stop": stop, "tp": tp, "entry_fee": fee}
        orders.append({"timestamp": ts, "kind": "entry", "action": d.action, "side": side, "quantity": qty,
                       "reference_price": open_price, "fill_price": fill, "fee": fee, "stop": stop,
                       "take_profit": tp, "notional": notional, "reason": d.reason})

    def _protective(self, i, ts, o, h, l, orders, trades, *, check_gap_only):
        p = self._pos
        stop, tp = p["stop"], p["tp"]
        if check_gap_only:
            # Position carried from the previous bar: only the open can already have crossed a level.
            res = resolve_bar_exit(p["side"], stop, tp, o, o, o)
        else:
            res = resolve_bar_exit(p["side"], stop, tp, o, h, l)
        if res is None:
            return
        reason, ref, ambiguous = res
        self._close(i, ts, ref, reason, ambiguous, orders, trades, market=reason.startswith("stop"))

    def _close(self, i, ts, ref, reason, ambiguous, orders, trades, *, market):
        costs, p = self.cfg.costs, self._pos
        side = p["side"]
        exit_fill = costs.fill_price(ref, -side, slippage=market)
        notional = p["qty"] * exit_fill
        fee = costs.fee(notional)
        gross = side * (exit_fill - p["entry"]) * p["qty"]
        self._balance += gross - fee
        friction = p["qty"] * (abs(p["entry"] - p["entry_ref"]) + abs(exit_fill - ref))
        fees = p["entry_fee"] + fee
        net = gross - fees
        entry_notional = p["qty"] * p["entry"]
        orders.append({"timestamp": ts, "kind": "exit", "side": -side, "quantity": p["qty"],
                       "reference_price": ref, "fill_price": exit_fill, "fee": fee, "reason": reason,
                       "ambiguous": ambiguous})
        trades.append({"entry_ts": p["entry_ts"], "exit_ts": ts, "side": side, "quantity": p["qty"],
                       "entry_price": p["entry"], "exit_price": exit_fill, "gross_pnl": gross,
                       "fees": fees, "spread_slippage_cost": friction, "net_pnl": net,
                       "net_return_pct": net / entry_notional * 100, "bars_held": i - p["bar"],
                       "exit_reason": reason, "ambiguous": ambiguous})
        self._pos = None
