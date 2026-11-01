"""Data quality checks for archived OHLC bars (free sources are not clean by default).

`check_bars` returns a report with a status: `pass`, `warn` (usable, with caveats) or `fail` (a study refuses the data). Findings:
* structure: duplicates, non-increasing timestamps, missing columns, OHLC inconsistencies, non-positive prices;
* coverage: calendar gaps, bars on days the instrument does not trade, history length;
* suspicious values: close-to-close and overnight-gap outliers (data errors, contract rolls or real events - listed, never silently removed),
  stale series (many identical closes), flat bars, zero volume (normal for spot FX, so informational there);
* freshness (age of the last bar).
Continuous futures from the free provider are NOT roll-adjusted: a contract roll appears as a gap between two contracts, which cannot be told
apart from a real overnight jump. Large overnight gaps are therefore listed as `roll_or_event_candidates`.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FAIL_RETURN = 0.60            # a single-day close-to-close move beyond 60 % is treated as a data error
WARN_RETURN = 0.25
GAP_LISTED = 0.08             # overnight gaps above 8 % are listed (roll or event)
MAX_CALENDAR_GAP_DAYS = 6


def _top(series: pd.Series, n: int = 5):
    s = series.abs().sort_values(ascending=False).head(n)
    return [{"date": str(i.date()), "value": float(series.loc[i])} for i in s.index]


def check_bars(frame: pd.DataFrame, *, interval: str = "1d", asset_class: str | None = None, min_bars: int = 500, now=None) -> dict:
    findings, status = [], "pass"

    def flag(level, msg):
        nonlocal status
        findings.append({"level": level, "message": msg})
        if level == "fail" or (level == "warn" and status == "pass"):
            status = "fail" if level == "fail" else "warn"

    rep = {"bars": int(len(frame))}
    need = {"Open", "High", "Low", "Close"}
    if not need <= set(frame.columns) or not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        flag("fail", "missing OHLC columns or timezone-aware datetime index")
        return {**rep, "status": status, "findings": findings}
    if len(frame) < min_bars:
        flag("fail", f"only {len(frame)} bars (< {min_bars})")
    idx = frame.index
    if idx.has_duplicates:
        flag("fail", f"{int(idx.duplicated().sum())} duplicate timestamps")
    if not idx.is_monotonic_increasing:
        flag("fail", "timestamps are not increasing")
    o, h, l, c = (frame[k].astype(float) for k in ("Open", "High", "Low", "Close"))
    if (frame[["Open", "High", "Low", "Close"]] <= 0).any().any() or frame[["Open", "High", "Low", "Close"]].isna().any().any():
        flag("fail", "non-positive or missing prices")
    bad = (h < np.maximum(o, c) - 1e-9 * h) | (l > np.minimum(o, c) + 1e-9 * l)
    if bad.any():
        flag("fail", f"{int(bad.sum())} bars where High/Low do not contain Open/Close")
    rep["first"], rep["last"] = str(idx[0].date()), str(idx[-1].date())
    rep["years"] = round((idx[-1] - idx[0]).days / 365.25, 2)

    ret = c.pct_change().dropna()
    gap = (o / c.shift(1) - 1).dropna()
    rep["daily_vol_pct"] = float(ret.std() * 100)
    worst = _top(ret)
    rep["largest_close_moves"] = worst
    if len(ret) and ret.abs().max() > FAIL_RETURN:
        flag("fail", f"a single-day move of {ret.abs().max():.0%} on {worst[0]['date']} looks like a data error")
    elif len(ret) and ret.abs().max() > WARN_RETURN:
        flag("warn", f"largest single-day move {ret.abs().max():.0%} on {worst[0]['date']} (real event or data error; kept)")
    rep["max_abs_overnight_gap_pct"] = float(gap.abs().max() * 100) if len(gap) else 0.0
    rep["gap_p999_pct"] = float(np.percentile(gap.abs(), 99.9) * 100) if len(gap) else 0.0
    cand = gap[gap.abs() > GAP_LISTED]
    rep["roll_or_event_candidates"] = [{"date": str(i.date()), "gap_pct": round(float(v) * 100, 2)} for i, v in cand.head(10).items()]
    rep["roll_or_event_candidate_count"] = int(len(cand))
    if len(cand):
        flag("warn", f"{len(cand)} overnight gaps above {GAP_LISTED:.0%} (contract rolls in unadjusted continuous futures or real events)")

    if interval == "1d" and len(idx) > 2:
        days = pd.Series(idx.normalize()).diff().dt.days.dropna()
        big = days[days > MAX_CALENDAR_GAP_DAYS]
        rep["calendar_gaps_over_6_days"] = int(len(big))
        if len(big):
            where = pd.Series(idx.normalize())[big.index]
            rep["largest_calendar_gaps"] = [{"before": str(w.date()), "days": int(d)} for w, d in zip(where.head(5), big.head(5))]
            flag("warn", f"{len(big)} calendar gaps longer than {MAX_CALENDAR_GAP_DAYS} days (missing data or non-trading periods)")
        wk = idx.day_of_week
        rep["weekend_bar_share"] = float((wk >= 5).mean())
        if asset_class not in ("fx", "crypto") and (wk >= 5).mean() > 0.02:
            flag("warn", f"{(wk >= 5).mean():.1%} of bars fall on weekends")
    bad = find_bad_bars(frame, asset_class=asset_class)
    rep["glitch_bars"] = [{"date": str(i.date()), "reason": ",".join(k for k in row.index if row[k])} for i, row in bad.head(12).iterrows()]
    rep["glitch_bar_count"] = int(len(bad))
    if len(bad):
        flag("fail" if len(bad) > 0.005 * len(frame) else "warn", f"{len(bad)} provider glitch bars (spike-and-revert or contradicting open); a study uses the cleaned view")
    stale = float((c.diff() == 0).mean())
    rep["identical_close_share"] = stale
    if stale > 0.05:
        flag("warn", f"{stale:.1%} of closes equal the previous close (stale feed?)")
    rep["flat_bar_share"] = float((h == l).mean())
    if rep["flat_bar_share"] > 0.05:
        flag("warn", f"{rep['flat_bar_share']:.1%} of bars have no range")
    if "Volume" in frame.columns:
        rep["zero_volume_share"] = float((frame["Volume"] == 0).mean())
        if rep["zero_volume_share"] > 0.5 and asset_class != "fx":
            flag("warn", f"{rep['zero_volume_share']:.0%} of bars have zero volume")
    ref = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    rep["last_bar_age_days"] = int((ref - idx[-1]).days)
    return {**rep, "status": status, "findings": findings}


SPIKE_MIN_MOVE = 0.05          # a one-day move beyond 5 % that is almost fully reversed the next day
SPIKE_REVERSAL = 0.30          # ... so that the two-day net move is below 30 % of the first
BAD_OPEN_GAP = 0.06            # open more than 6 % from the previous close while the bar's close is near it


SPIKE_SIGMAS = 6.0             # ... and beyond this many times the instrument's own recent daily volatility


def find_bad_bars(frame: pd.DataFrame, *, asset_class: str | None = None) -> pd.DataFrame:
    """Bars that are almost certainly provider glitches: one-day spikes that revert, and opens that contradict both neighbours.
    A move only counts if it is large in absolute terms AND many times the instrument's own recent volatility (60 past days), so
    genuine crash/limit days in volatile regimes are kept; genuine moves that persist are never flagged because they do not revert."""
    c, o = frame["Close"].astype(float), frame["Open"].astype(float)
    r = c.pct_change()
    sigma = r.rolling(60, min_periods=20).std().shift(1)
    nxt = r.shift(-1)
    big = (r.abs() > SPIKE_MIN_MOVE) & (r.abs() > SPIKE_SIGMAS * sigma)
    spike = big & (nxt * r < 0) & ((r + nxt).abs() < SPIKE_REVERSAL * r.abs())
    gap = o / c.shift(1) - 1
    bad_open = (gap.abs() > BAD_OPEN_GAP) & (gap.abs() > SPIKE_SIGMAS * sigma) & (r.abs() < 0.35 * gap.abs())
    out = pd.DataFrame({"spike_reversal": spike.fillna(False), "bad_open": bad_open.fillna(False)})
    return out[out.any(axis=1)]


def clean_bars(frame: pd.DataFrame, *, asset_class: str | None = None) -> tuple[pd.DataFrame, list[dict]]:
    """Returns (frame without glitch bars, removed). The archive itself is never modified."""
    bad = find_bad_bars(frame, asset_class=asset_class)
    removed = [{"date": str(i.date()), "reason": ",".join(k for k in row.index if row[k])} for i, row in bad.iterrows()]
    return frame.drop(index=bad.index), removed


def usable_history(frame: pd.DataFrame, *, min_bar_share: float = 0.9, max_flat_share: float = 0.10, max_gap_days: int = 10, tolerance: float = 0.8) -> dict:
    """From which date is a free series dense enough to trust? Per COMPLETE calendar year: enough bars (>= 90 % of the median of the latest years), few
    bars without any range (open = high = low = close: the provider gave a settlement only, so intrabar stops cannot be simulated) and no long gap.
    Usable history starts at the first passing year from which at least 80 % of the following years also pass. `usable` is False if no year qualifies."""
    g = frame.groupby(frame.index.year)
    years = sorted(g.groups)
    full = [y for y in years if y not in (years[0], years[-1])]       # first and last calendar years are partial
    if len(full) < 3:
        return {"usable": False, "reason": "fewer than three complete years"}
    n = g.size()
    expected = float(np.median([n[y] for y in full[-5:]]))
    flat = g.apply(lambda d: float(((d["High"] == d["Low"]) & (d["Open"] == d["Close"])).mean()))
    gap = g.apply(lambda d: float(pd.Series(d.index.normalize()).diff().dt.days.max() or 0))
    table = {int(y): {"bars": int(n[y]), "flat_share": round(flat[y], 3), "max_gap_days": int(gap[y]), "pass": bool(n[y] >= min_bar_share * expected and flat[y] <= max_flat_share and gap[y] <= max_gap_days)} for y in full}
    ys = sorted(table)
    start = None
    for k, y in enumerate(ys):
        rest = ys[k:]
        if table[y]["pass"] and np.mean([table[z]["pass"] for z in rest]) >= tolerance:
            start = y
            break
    if start is None:
        return {"usable": False, "reason": "no year from which the series is dense enough", "years": table}
    first = frame.index[frame.index.year >= start][0]
    usable = frame.loc[first:]
    flat_all = float(((usable["High"] == usable["Low"]) & (usable["Open"] == usable["Close"])).mean())
    return {"usable": True, "first_usable_date": str(first.date()), "bars_usable": int(len(usable)), "usable_share": float(len(usable) / len(frame)),
            "flat_share_in_usable": flat_all, "expected_bars_per_year": expected, "years": table}


def compare_overlap(old: pd.DataFrame, new: pd.DataFrame) -> dict:
    """How well two downloads of the same series agree on the dates they share (revisions, adjustments)."""
    common = old.index.intersection(new.index)
    if len(common) == 0:
        return {"common_bars": 0}
    d = (new.loc[common, "Close"] / old.loc[common, "Close"] - 1).abs()
    return {"common_bars": int(len(common)), "max_close_diff_pct": float(d.max() * 100), "share_identical": float((d < 1e-9).mean())}
