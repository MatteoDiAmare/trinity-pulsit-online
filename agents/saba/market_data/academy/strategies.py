"""Strategies: pure decision logic. They see only completed bars up to 'now'.

A strategy never touches prices after the current bar: MarketView exposes read-only
numpy slices ending at the current bar, and the simulator fills at the next open.
Strategies are rebuilt for each run (build_strategy) so internal state never leaks.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np

ACTIONS = ("BUY", "SELL", "CLOSE", "ABSTAIN")
DEFAULT_STOP_PCT = 2.0


def _mandatory_stop(value):
    """Every strategy must carry a stop-loss; None/0 is a configuration error, not a default."""
    if value is None or not 0 < value < 100:
        raise ValueError("stop_loss_pct is mandatory and must be in (0, 100)")
    return float(value)


@dataclass(frozen=True)
class Decision:
    action: str = "ABSTAIN"
    size: float = 1.0                    # fraction (0,1] of the allowed exposure
    stop_loss_pct: float | None = None   # percent of fill price, e.g. 1.0 = 1 %
    take_profit_pct: float | None = None
    reason: str = ""

    def __post_init__(self):
        if self.action not in ACTIONS:
            raise ValueError(f"invalid action {self.action}")
        if not 0 < self.size <= 1:
            raise ValueError("size must be in (0, 1]")
        for v in (self.stop_loss_pct, self.take_profit_pct):
            if v is not None and not 0 < v < 100:
                raise ValueError("stop/take-profit percent must be in (0, 100)")


@dataclass(frozen=True)
class PositionInfo:
    side: int  # +1 long, -1 short
    entry_price: float
    quantity: float
    bars_held: int


MAX_LOOKBACK = 3000   # bars exposed to a strategy; longer histories are truncated to the most recent bars


class MarketView:
    """Read-only window of completed bars ending at the current bar.

    The arrays are independent COPIES (owndata, read-only): a view onto the full series would leak
    future bars through `array.base`. `n` is the number of bars in the window (<= MAX_LOOKBACK),
    `total` the number of bars seen so far. Adversarial in-process code can still reach other objects via
    gc/frames; arbitrary custom-code strategies therefore need the isolated runner, not this class alone.
    """
    __slots__ = ("open", "high", "low", "close", "volume", "timestamp", "n", "total")

    def __init__(self, arrays: dict, index, n: int):
        lo = max(0, n - MAX_LOOKBACK)
        if "_stack" in arrays:       # (5, N) matrix prepared once by the simulator: one copy per bar
            block = np.array(arrays["_stack"][:, lo:n], copy=True)
        else:
            block = np.array([arrays[k][lo:n] for k in ("open", "high", "low", "close", "volume")], dtype=float)
        block.setflags(write=False)
        self.open, self.high, self.low, self.close, self.volume = block   # rows: base is this window-only copy
        self.timestamp = index[n - 1]
        self.n, self.total = n - lo, n

    def __len__(self):
        return self.n


class Strategy:
    name = "base"

    def params(self) -> dict:
        return {}

    def on_bar(self, view: MarketView, position: PositionInfo | None) -> Decision:  # pragma: no cover
        raise NotImplementedError


def _flip(desired: int, position, risk: dict, allow_short: bool, reason: str) -> Decision:
    """Common logic: move toward desired side (+1/-1)."""
    if desired == -1 and not allow_short:
        return Decision("CLOSE", reason=reason) if position else Decision()
    if position is not None and position.side == desired:
        return Decision()
    return Decision("BUY" if desired == 1 else "SELL", reason=reason, **risk)


class SmaCross(Strategy):
    name = "sma_cross"

    def __init__(self, short=10, long=30, stop_loss_pct=DEFAULT_STOP_PCT, take_profit_pct=None, allow_short=True):
        if not 2 <= short < long:
            raise ValueError("require 2 <= short < long")
        self.short, self.long, self.allow_short = int(short), int(long), bool(allow_short)
        self.sl, self.tp = _mandatory_stop(stop_loss_pct), take_profit_pct

    def params(self):
        return {"short": self.short, "long": self.long, "stop_loss_pct": self.sl,
                "take_profit_pct": self.tp, "allow_short": self.allow_short}

    def on_bar(self, view, position):
        if view.n < self.long:
            return Decision()
        fast, slow = view.close[-self.short:].mean(), view.close[-self.long:].mean()
        if fast == slow:
            return Decision()
        return _flip(1 if fast > slow else -1, position,
                     {"stop_loss_pct": self.sl, "take_profit_pct": self.tp}, self.allow_short, "sma_cross")


class Breakout(Strategy):
    """Donchian breakout of the previous `window` bars (current bar excluded from the channel)."""
    name = "breakout"

    def __init__(self, window=20, stop_loss_pct=DEFAULT_STOP_PCT, take_profit_pct=None, allow_short=True):
        if window < 2:
            raise ValueError("window must be >= 2")
        self.window, self.allow_short = int(window), bool(allow_short)
        self.sl, self.tp = _mandatory_stop(stop_loss_pct), take_profit_pct

    def params(self):
        return {"window": self.window, "stop_loss_pct": self.sl, "take_profit_pct": self.tp,
                "allow_short": self.allow_short}

    def on_bar(self, view, position):
        if view.n < self.window + 1:
            return Decision()
        upper = view.high[-self.window - 1:-1].max()
        lower = view.low[-self.window - 1:-1].min()
        close = view.close[-1]
        risk = {"stop_loss_pct": self.sl, "take_profit_pct": self.tp}
        if close > upper:
            return _flip(1, position, risk, self.allow_short, "breakout_up")
        if close < lower:
            return _flip(-1, position, risk, self.allow_short, "breakout_down")
        return Decision()


class MeanReversion(Strategy):
    name = "mean_reversion"

    def __init__(self, window=20, z_entry=2.0, stop_loss_pct=DEFAULT_STOP_PCT, take_profit_pct=None, allow_short=True):
        if window < 3 or z_entry <= 0:
            raise ValueError("window >= 3 and z_entry > 0 required")
        self.window, self.z_entry, self.allow_short = int(window), float(z_entry), bool(allow_short)
        self.sl, self.tp = _mandatory_stop(stop_loss_pct), take_profit_pct

    def params(self):
        return {"window": self.window, "z_entry": self.z_entry, "stop_loss_pct": self.sl,
                "take_profit_pct": self.tp, "allow_short": self.allow_short}

    def on_bar(self, view, position):
        if view.n < self.window:
            return Decision()
        win = view.close[-self.window:]
        sd = win.std()
        if sd == 0:
            return Decision()
        z = (win[-1] - win.mean()) / sd
        if position is not None:
            if (position.side == 1 and z >= 0) or (position.side == -1 and z <= 0):
                return Decision("CLOSE", reason="reverted_to_mean")
            return Decision()
        risk = {"stop_loss_pct": self.sl, "take_profit_pct": self.tp}
        if z < -self.z_entry:
            return Decision("BUY", reason="oversold", **risk)
        if z > self.z_entry and self.allow_short:
            return Decision("SELL", reason="overbought", **risk)
        return Decision()


class RandomControl(Strategy):
    """Seeded coin-flip control: any apparent edge of real strategies must beat this."""
    name = "random_control"

    def __init__(self, seed=0, trade_prob=0.02, hold_bars=10, stop_loss_pct=DEFAULT_STOP_PCT, take_profit_pct=None):
        if not 0 <= trade_prob <= 1 or hold_bars < 1:
            raise ValueError("invalid random control parameters")
        self.seed, self.trade_prob, self.hold_bars = int(seed), float(trade_prob), int(hold_bars)
        self.sl, self.tp = _mandatory_stop(stop_loss_pct), take_profit_pct
        self.rng = np.random.default_rng(self.seed)

    def params(self):
        return {"seed": self.seed, "trade_prob": self.trade_prob, "hold_bars": self.hold_bars,
                "stop_loss_pct": self.sl, "take_profit_pct": self.tp}

    def on_bar(self, view, position):
        draw, side_draw = self.rng.random(), self.rng.random()  # constant draws per call
        if position is not None:
            return Decision("CLOSE", reason="random_timeout") if position.bars_held >= self.hold_bars else Decision()
        if draw < self.trade_prob:
            return Decision("BUY" if side_draw < 0.5 else "SELL", reason="random_entry",
                            stop_loss_pct=self.sl, take_profit_pct=self.tp)
        return Decision()


STRATEGIES = {c.name: c for c in (SmaCross, Breakout, MeanReversion, RandomControl)}


def build_strategy(name: str, params: dict | None = None) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(f"unknown strategy {name}; choose from {sorted(STRATEGIES)}")
    return STRATEGIES[name](**(params or {}))
