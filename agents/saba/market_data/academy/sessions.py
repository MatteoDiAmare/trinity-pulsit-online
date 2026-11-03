"""Market sessions: when a market is open, which bars should exist, and which gaps are scheduled closures versus missing data.

Why: daily bars hide the difference between a 24/7 market (crypto), a 24/5 market (spot FX), a 23-hour futures market with a daily break (gold on
Globex) and a market with limited hours (equity index, regular trading hours). Intraday studies must know which bars to expect, otherwise a weekend
looks like a data gap and a missing hour looks like a closure.

A session is a *kind* (data, not code): `24x7`, `fx_24x5`, `cme_globex`, `rth_us`. Each resolves to a timezone and, per local weekday, a list of
(open, close) windows in minutes since local midnight (close may be 1440). Daylight saving is handled by working in the local timezone.
`expected_index` lists the bar START timestamps (UTC) that a complete series would contain; `check_session` compares that with the data.
"""
from __future__ import annotations

import pandas as pd

MINUTES = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1h": 60, "90m": 90}
INTRADAY = tuple(MINUTES)


def interval_minutes(interval: str) -> int | None:
    return MINUTES.get(interval)


def is_intraday(interval: str) -> bool:
    return interval in MINUTES


def _windows(kind: str):
    """-> (timezone, {weekday(0=Mon): [(open_min, close_min), ...]})"""
    if kind == "24x7":
        return "UTC", {d: [(0, 1440)] for d in range(7)}
    if kind == "fx_24x5":      # Sun 17:00 New York -> Fri 17:00 New York
        w = {d: [(0, 1440)] for d in range(1, 4)}
        w[0] = [(0, 1440)]
        w[4] = [(0, 17 * 60)]
        w[5] = []
        w[6] = [(17 * 60, 1440)]
        return "America/New_York", w
    if kind == "cme_globex":   # Sun 18:00 -> Fri 17:00 New York, daily break 17:00-18:00 Mon-Thu
        w = {d: [(0, 17 * 60), (18 * 60, 1440)] for d in range(0, 4)}
        w[4] = [(0, 17 * 60)]
        w[5] = []
        w[6] = [(18 * 60, 1440)]
        return "America/New_York", w
    if kind == "rth_us":       # regular trading hours of US equities / equity-index cash session
        w = {d: [(9 * 60 + 30, 16 * 60)] for d in range(5)}
        w[5], w[6] = [], []
        return "America/New_York", w
    raise ValueError(f"unknown session kind {kind!r}; known: 24x7, fx_24x5, cme_globex, rth_us")


KINDS = ("24x7", "fx_24x5", "cme_globex", "rth_us")


def expected_index(kind: str, start, end, interval: str) -> pd.DatetimeIndex:
    """Start timestamps (UTC) of every bar a complete series of `kind` would hold between start and end (inclusive of bars that START within the range)."""
    step = interval_minutes(interval)
    if step is None:
        raise ValueError(f"{interval!r} is not an intraday interval")
    tz, windows = _windows(kind)
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    parts = []
    # calendar arithmetic on NAIVE local dates: subtracting 24 h from a tz-aware midnight drifts by an hour across a daylight-saving change
    first = start.tz_convert(tz).tz_localize(None).normalize() - pd.Timedelta(days=1)
    last = end.tz_convert(tz).tz_localize(None).normalize() + pd.Timedelta(days=1)
    for day in pd.date_range(first, last, freq="D"):
        for o, c in windows[day.weekday()]:
            if c - o < step:
                continue
            n = (c - o) // step
            local = day + pd.Timedelta(minutes=o) + pd.to_timedelta(range(0, n * step, step), unit="m")
            parts.append(pd.DatetimeIndex(local).tz_localize(tz, ambiguous="NaT", nonexistent="NaT"))
    if not parts:
        return pd.DatetimeIndex([], tz="UTC")
    idx = parts[0].append(parts[1:]) if len(parts) > 1 else parts[0]
    idx = idx[~idx.isna()].tz_convert("UTC")
    return idx[(idx >= start) & (idx <= end)].sort_values().unique()


def in_session(index: pd.DatetimeIndex, kind: str, interval: str) -> pd.Series:
    """Boolean per timestamp: does a bar starting there belong to the session?"""
    exp = expected_index(kind, index[0], index[-1], interval)
    return pd.Series(index.isin(exp), index=index)


def _dilate(mask, k):
    """True where any element within k positions (either side) is True."""
    import numpy as np
    if k <= 0:
        return mask.copy()
    c = np.concatenate([[0], np.cumsum(mask.astype(int))])
    n = len(mask)
    lo = np.clip(np.arange(n) - k, 0, n)
    hi = np.clip(np.arange(n) + k + 1, 0, n)
    return (c[hi] - c[lo]) > 0


def check_session(frame: pd.DataFrame, kind: str, interval: str, *, boundary_tolerance_min: int = 60, now=None) -> dict:
    """Compare the bars with the session calendar. Status: pass / warn / fail with plain findings.

    * completeness: share of expected bars present (missing bars INSIDE sessions are data gaps; closures are not);
    * unscheduled gaps: the longest runs of consecutive expected bars that are missing;
    * outside-session bars: data from times the market is closed (e.g. pre-market in a regular-hours series, or wrong timestamps/timezone).

    `boundary_tolerance_min`: bars within this distance of a scheduled opening/closing are NOT judged. Providers disagree with the venue's own clock by up to an
    hour around daylight-saving changes (e.g. Yahoo closes spot FX at a fixed UTC time while the New-York convention moves with DST); such bars are counted
    separately as `boundary_bars`. Public holidays are not modelled: a holiday shows up as a missing run (a warning, never a failure by itself)."""
    import numpy as np
    if frame.empty:
        return {"status": "fail", "findings": [{"level": "fail", "message": "no bars"}]}
    step = interval_minutes(interval)
    k = int(boundary_tolerance_min // step)
    exp = expected_index(kind, frame.index[0], frame.index[-1], interval)
    have = frame.index
    grid = pd.date_range(min(exp[0], have[0]) if len(exp) else have[0], max(exp[-1], have[-1]) if len(exp) else have[-1], freq=f"{step}min", tz="UTC")
    in_exp, in_have = grid.isin(exp), grid.isin(have)
    near_closed, near_open = _dilate(~in_exp, k), _dilate(in_exp, k)
    miss_all = in_exp & ~in_have
    miss = miss_all & ~near_closed                       # a missing bar right next to a closure is boundary ambiguity, not a gap
    out_all = ~in_exp & in_have
    out = out_all & ~near_open
    n_exp = int((in_exp & ~near_closed).sum())
    comp = 1 - int(miss.sum()) / max(n_exp, 1)
    longest, cur = 0, 0
    for m in miss_all & ~near_closed:
        cur = cur + 1 if m else 0
        longest = max(longest, cur)
    # runs are counted over the expected bars only (a closure inside a run of missing bars interrupts it)
    findings, status = [], "pass"

    def flag(level, msg):
        nonlocal status
        findings.append({"level": level, "message": msg})
        status = "fail" if level == "fail" or status == "fail" else "warn"

    if comp < 0.80:
        flag("fail", f"only {comp:.1%} of the bars the {kind} session should contain are present")
    elif comp < 0.97:
        flag("warn", f"{comp:.1%} of expected {kind} bars present ({int(miss.sum())} missing)")
    if longest * step >= 6 * 60:
        flag("warn", f"longest run of missing expected bars is {longest} ({longest * step / 60:.1f} h): a data hole or a public holiday (holidays are not modelled)")
    if int(out.sum()) / max(len(have), 1) > 0.005:
        flag("warn", f"{int(out.sum())} bars ({int(out.sum()) / len(have):.1%}) fall outside the {kind} session: wrong timezone, pre/post-market data, or holiday bars")
    return {"status": status, "kind": kind, "interval": interval, "expected_bars": n_exp, "present_expected_bars": n_exp - int(miss.sum()),
            "missing_expected_bars": int(miss.sum()), "completeness": round(comp, 4), "longest_missing_run_bars": int(longest),
            "outside_session_bars": int(out.sum()), "boundary_bars": int((miss_all & near_closed).sum() + (out_all & near_open).sum()),
            "boundary_tolerance_min": boundary_tolerance_min, "findings": findings}
