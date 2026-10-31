"""JSON-defined strategies: Saba composes strategies as DATA; this module interprets them safely.

No code is executed: a spec is validated against a closed grammar (whitelisted indicator types, operators,
bounded sizes), then evaluated on read-only completed bars. Every indicator is computed from bars up to
the current one, so a spec cannot look ahead by construction (this is still verified by causality.py).

Spec shape (all keys optional unless marked):
{
  "name": "ema_trend", "version": 1, "hypothesis": "free text",
  "indicators": {"fast": {"type": "ema", "source": "close", "window": 10}, ...},
  "entry_long":  {"all": [{"gt": ["fast", "slow"]}, {"lt": ["rsi", 70]}]},
  "entry_short": {...}, "exit_long": {...}, "exit_short": {...},
  "allow_short": true, "size": 1.0,
  "risk": {"stop_loss_pct": 2.0,           # REQUIRED: maximum stop distance
           "stop_atr_mult": 2.0, "atr_indicator": "atr", "stop_floor_pct": 0.1, "take_profit_pct": 4.0}
}
Indicator types: sma, ema, stdev, zscore, roc, rsi, atr, highest, lowest (window incl. current bar unless
"exclude_current": true), hlc3_sma.  Sources: open high low close volume hl2 hlc3 range ret.
Operands: number | raw name (open high low close volume hour_utc weekday) | indicator name |
          {"prev": [name, k]} (k bars back, 1..50) | {"add"|"sub"|"mul"|"div"|"min"|"max": [a, b]} | {"abs": a}.
Conditions: gt lt ge le [a, b]; cross_above / cross_below [a, b]; all / any [conds]; not cond.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

import numpy as np

from .strategies import Decision, STRATEGIES, Strategy, _mandatory_stop

RAW = ("open", "high", "low", "close", "volume")
TIME_FEATURES = ("hour_utc", "weekday")
SOURCES = RAW + ("hl2", "hlc3", "range", "ret")
INDICATOR_TYPES = ("sma", "ema", "stdev", "zscore", "roc", "rsi", "atr", "highest", "lowest", "hlc3_sma")
MAX_WINDOW, MAX_INDICATORS, MAX_NODES, MAX_DEPTH, MAX_PREV = 500, 20, 200, 8, 50
TOP_KEYS = {"name", "version", "hypothesis", "indicators", "entry_long", "entry_short", "exit_long",
            "exit_short", "allow_short", "size", "risk"}
RISK_KEYS = {"stop_loss_pct", "stop_atr_mult", "atr_indicator", "stop_floor_pct", "take_profit_pct"}
CMP = {"gt": lambda a, b: a > b, "lt": lambda a, b: a < b, "ge": lambda a, b: a >= b, "le": lambda a, b: a <= b}
ARITH = {"add": lambda a, b: a + b, "sub": lambda a, b: a - b, "mul": lambda a, b: a * b,
         "div": lambda a, b: a / b if b != 0 else float("nan"), "min": min, "max": max}
NAN = float("nan")


class SpecError(ValueError):
    pass


def _count(node, depth, ctx, names):
    ctx["nodes"] += 1
    if ctx["nodes"] > MAX_NODES or depth > MAX_DEPTH:
        raise SpecError("expression too large or too deep")
    if isinstance(node, bool):
        raise SpecError("booleans are not operands")
    if isinstance(node, (int, float)):
        if not math.isfinite(node):
            raise SpecError("non-finite number")
        return
    if isinstance(node, str):
        if node not in RAW and node not in TIME_FEATURES and node not in names:
            raise SpecError(f"unknown operand {node!r}")
        return
    if not isinstance(node, dict) or len(node) != 1:
        raise SpecError("each expression must be a number, a name or a single-key object")
    (op, arg), = node.items()
    if op == "prev":
        if not (isinstance(arg, list) and len(arg) == 2 and isinstance(arg[0], str) and isinstance(arg[1], int)
                and 1 <= arg[1] <= MAX_PREV):
            raise SpecError("prev needs [name, k] with 1 <= k <= %d" % MAX_PREV)
        if arg[0] in TIME_FEATURES:
            raise SpecError("time features cannot be lagged")
        _count(arg[0], depth + 1, ctx, names)
    elif op in ARITH or op in CMP or op in ("cross_above", "cross_below"):
        if not (isinstance(arg, list) and len(arg) == 2):
            raise SpecError(f"{op} needs exactly two operands")
        for a in arg:
            _count(a, depth + 1, ctx, names)
    elif op == "abs":
        _count(arg, depth + 1, ctx, names)
    elif op in ("all", "any"):
        if not (isinstance(arg, list) and 1 <= len(arg) <= 20):
            raise SpecError(f"{op} needs 1-20 conditions")
        for a in arg:
            _count(a, depth + 1, ctx, names)
    elif op == "not":
        _count(arg, depth + 1, ctx, names)
    else:
        raise SpecError(f"unknown operator {op!r}")


def validate_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise SpecError("spec must be an object")
    extra = set(spec) - TOP_KEYS
    if extra:
        raise SpecError(f"unknown keys: {sorted(extra)}")
    inds = spec.get("indicators", {})
    if not isinstance(inds, dict) or len(inds) > MAX_INDICATORS:
        raise SpecError("indicators must be an object with at most %d entries" % MAX_INDICATORS)
    for name, ind in inds.items():
        if not isinstance(name, str) or not name.isidentifier() or name in RAW or name in TIME_FEATURES:
            raise SpecError(f"invalid indicator name {name!r}")
        if not isinstance(ind, dict) or ind.get("type") not in INDICATOR_TYPES:
            raise SpecError(f"indicator {name}: type must be one of {INDICATOR_TYPES}")
        if set(ind) - {"type", "source", "window", "exclude_current"}:
            raise SpecError(f"indicator {name}: unknown keys")
        w = ind.get("window")
        if not isinstance(w, int) or isinstance(w, bool) or not 2 <= w <= MAX_WINDOW:
            raise SpecError(f"indicator {name}: window must be an integer in [2, {MAX_WINDOW}]")
        if ind.get("source", "close") not in SOURCES:
            raise SpecError(f"indicator {name}: source must be one of {SOURCES}")
    names = set(inds)
    ctx = {"nodes": 0}
    rules = [k for k in ("entry_long", "entry_short") if k in spec]
    if not rules:
        raise SpecError("at least one of entry_long / entry_short is required")
    for key in ("entry_long", "entry_short", "exit_long", "exit_short"):
        if key in spec:
            _count(spec[key], 0, ctx, names)
    risk = spec.get("risk")
    if not isinstance(risk, dict) or set(risk) - RISK_KEYS:
        raise SpecError("risk object with known keys is required")
    try:
        risk["stop_loss_pct"] = _mandatory_stop(risk.get("stop_loss_pct"))
    except ValueError as exc:
        raise SpecError(str(exc)) from None
    for key in ("stop_atr_mult", "stop_floor_pct", "take_profit_pct"):
        if risk.get(key) is not None and not 0 < risk[key] < 100:
            raise SpecError(f"risk.{key} must be in (0, 100)")
    if risk.get("stop_atr_mult") is not None and inds.get(risk.get("atr_indicator"), {}).get("type") != "atr":
        raise SpecError("stop_atr_mult needs atr_indicator naming an 'atr' indicator")
    size = spec.get("size", 1.0)
    if not isinstance(size, (int, float)) or not 0 < size <= 1:
        raise SpecError("size must be in (0, 1]")
    if len(json.dumps(spec)) > 20_000:
        raise SpecError("spec too large")
    return spec


def _max_prev(node) -> int:
    if isinstance(node, dict):
        (op, arg), = node.items()
        if op == "prev":
            return arg[1]
        return max((_max_prev(a) for a in (arg if isinstance(arg, list) else [arg])), default=0)
    return 0


def warmup_bars(spec: dict) -> int:
    inds = spec.get("indicators", {}).values()
    w = max([i["window"] * (5 if i["type"] in ("ema", "rsi", "atr") else 1) + 1 for i in inds] + [2])
    prev = max([_max_prev(spec[k]) for k in ("entry_long", "entry_short", "exit_long", "exit_short") if k in spec] + [1])
    return w + prev


@lru_cache(maxsize=64)
def _ew(window: int, alpha_kind: str) -> np.ndarray:
    alpha = 2 / (window + 1) if alpha_kind == "ema" else 1 / window
    k = np.arange(5 * window)[::-1]
    w = (1 - alpha) ** k
    return w / w.sum()


class _Eval:
    def __init__(self, view, spec):
        self.v, self.spec, self.memo = view, spec, {}

    def series(self, source, end):
        v = self.v
        if source in RAW:
            return getattr(v, source)[:end]
        if source == "hl2":
            return (v.high[:end] + v.low[:end]) / 2
        if source == "hlc3":
            return (v.high[:end] + v.low[:end] + v.close[:end]) / 3
        if source == "range":
            return (v.high[:end] - v.low[:end]) / np.where(v.close[:end] != 0, v.close[:end], 1) * 100
        c = v.close[:end]
        r = np.full(len(c), NAN)
        r[1:] = c[1:] / np.where(c[:-1] != 0, c[:-1], 1) - 1
        return r

    def indicator(self, name, shift):
        key = (name, shift)
        if key in self.memo:
            return self.memo[key]
        ind = self.spec["indicators"][name]
        end = self.v.n - shift
        w, t = ind["window"], ind["type"]
        val = NAN
        if t == "atr":
            need = min(end, 5 * w + 1)
            if end >= w + 1:
                h, l, c = self.v.high[end - need:end], self.v.low[end - need:end], self.v.close[end - need:end]
                tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
                val = float(np.dot(_ew(w, "wilder"), tr[-5 * w:])) if len(tr) >= 5 * w else float(tr[-w:].mean())
        else:
            src = ind.get("source", "close") if t != "hlc3_sma" else "hlc3"
            a = self.series(src, end)
            if t in ("sma", "hlc3_sma"):
                val = float(a[-w:].mean()) if len(a) >= w else NAN
            elif t == "stdev":
                val = float(a[-w:].std()) if len(a) >= w else NAN
            elif t == "zscore":
                if len(a) >= w:
                    win = a[-w:]
                    sd = win.std()
                    val = float((win[-1] - win.mean()) / sd) if sd > 0 else NAN
            elif t == "roc":
                val = float(a[-1] / a[-1 - w] - 1) if len(a) > w and a[-1 - w] != 0 else NAN
            elif t in ("highest", "lowest"):
                lo = -w - 1 if ind.get("exclude_current") else -w
                win = a[lo:-1] if ind.get("exclude_current") else a[lo:]
                if len(win) >= w and not np.isnan(win).any():
                    val = float(win.max() if t == "highest" else win.min())
            elif t == "ema":
                if len(a) >= 5 * w:
                    val = float(np.dot(_ew(w, "ema"), a[-5 * w:]))
            elif t == "rsi":
                if len(a) >= 5 * w + 1:
                    d = np.diff(a[-5 * w - 1:])
                    g, lo_ = np.dot(_ew(w, "wilder"), np.maximum(d, 0)), np.dot(_ew(w, "wilder"), np.maximum(-d, 0))
                    val = 100.0 if lo_ == 0 and g > 0 else (50.0 if lo_ == 0 else float(100 - 100 / (1 + g / lo_)))
        self.memo[key] = val
        return val

    def value(self, node, shift=0):
        if isinstance(node, (int, float)):
            return float(node)
        v = self.v
        if isinstance(node, str):
            return self.name_value(node, shift)
        (op, arg), = node.items()
        if op == "prev":
            return self.name_value(arg[0], shift + arg[1])
        if op == "abs":
            return abs(self.value(arg, shift))
        a, b = self.value(arg[0], shift), self.value(arg[1], shift)
        if math.isnan(a) or math.isnan(b):
            return NAN
        return float(ARITH[op](a, b))

    def name_value(self, name, shift):
        v = self.v
        if name in TIME_FEATURES:
            ts = v.timestamp
            return float(ts.hour if name == "hour_utc" else ts.weekday())
        if v.n - shift < 1:
            return NAN
        if name in RAW:
            return float(getattr(v, name)[v.n - shift - 1])
        return self.indicator(name, shift)

    def cond(self, node, shift=0):
        (op, arg), = node.items()
        if op in CMP:
            a, b = self.value(arg[0], shift), self.value(arg[1], shift)
            return not (math.isnan(a) or math.isnan(b)) and CMP[op](a, b)
        if op in ("cross_above", "cross_below"):
            a0, b0 = self.value(arg[0], shift), self.value(arg[1], shift)
            a1, b1 = self.value(arg[0], shift + 1), self.value(arg[1], shift + 1)
            if any(math.isnan(x) for x in (a0, b0, a1, b1)):
                return False
            return (a1 <= b1 and a0 > b0) if op == "cross_above" else (a1 >= b1 and a0 < b0)
        if op == "all":
            return all(self.cond(c, shift) for c in arg)
        if op == "any":
            return any(self.cond(c, shift) for c in arg)
        if op == "not":
            return not self.cond(arg, shift)
        raise SpecError(f"{op} is not a condition")


class SpecStrategy(Strategy):
    name = "spec"

    def __init__(self, spec: dict):
        self.spec = validate_spec(json.loads(json.dumps(spec)))      # deep copy, validated
        self.risk = self.spec["risk"]
        self.allow_short = bool(self.spec.get("allow_short", True))
        self.size = float(self.spec.get("size", 1.0))
        self.warmup = warmup_bars(self.spec)

    def params(self):
        return {"spec": self.spec}

    def _risk(self, ev, view):
        stop = self.risk["stop_loss_pct"]
        if self.risk.get("stop_atr_mult") is not None:
            atr = ev.indicator(self.risk["atr_indicator"], 0)
            if not math.isnan(atr) and view.close[-1] > 0:
                atr_pct = self.risk["stop_atr_mult"] * atr / view.close[-1] * 100
                stop = min(stop, max(atr_pct, self.risk.get("stop_floor_pct", 0.1)))
        return {"stop_loss_pct": min(max(stop, 1e-6), 99.0), "take_profit_pct": self.risk.get("take_profit_pct"),
                "size": self.size}

    def on_bar(self, view, position):
        if view.n < self.warmup:
            return Decision()
        s, ev = self.spec, _Eval(view, self.spec)
        go_long = "entry_long" in s and ev.cond(s["entry_long"])
        go_short = "entry_short" in s and self.allow_short and ev.cond(s["entry_short"])
        if position is not None:
            out = s.get("exit_long" if position.side == 1 else "exit_short")
            if out is not None and ev.cond(out):
                return Decision("CLOSE", reason="spec_exit")
            if position.side == 1 and go_short and not go_long:
                return Decision("SELL", reason="spec_reverse", **self._risk(ev, view))
            if position.side == -1 and go_long and not go_short:
                return Decision("BUY", reason="spec_reverse", **self._risk(ev, view))
            return Decision()
        if go_long == go_short:
            return Decision()                     # nothing, or conflicting signals
        return Decision("BUY" if go_long else "SELL", reason="spec_entry", **self._risk(ev, view))


STRATEGIES["spec"] = SpecStrategy


def load_spec_file(path) -> dict:
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_spec(spec)


def load_specs_dir(directory) -> dict[str, dict]:
    out = {}
    for p in sorted(Path(directory).glob("*.json")):
        spec = load_spec_file(p)
        out[spec.get("name", p.stem)] = spec
    return out
