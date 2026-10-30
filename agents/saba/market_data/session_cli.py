"""Read-only daily session and late-entry risk report for Saba."""
from __future__ import annotations
import argparse
import json
import pandas as pd
from .archive import MarketArchive
from .sessions import session_labels, prior_asia_range
from .intraday_policy import assess_late_session


def research_snapshot(bars: pd.DataFrame, *, holding_minutes: float = 20.0, buffer_minutes: float = 5.0) -> dict:
    if bars.empty:
        raise ValueError("No bars")
    labels = session_labels(bars.index)
    ranges = prior_asia_range(bars)
    last = bars.index[-1]
    context = labels.iloc[-1]
    asia = ranges.iloc[-1]
    # Source timestamps are bar starts. Decision can occur only after completion.
    completed_at = (last + pd.Timedelta(minutes=1)).to_pydatetime()
    risk = assess_late_session(completed_at, holding_minutes, buffer_minutes)
    return {
        "as_of_completed_utc": completed_at.isoformat(),
        "london_local": context["london_local"],
        "new_york_local": context["ny_local"],
        "london_open_window": bool(context["london_open_window"]),
        "ny_cash_open_window": bool(context["ny_cash_open_window"]),
        "asia_reference_high": float(asia["asia_high"]) if pd.notna(asia["asia_high"]) else None,
        "asia_reference_low": float(asia["asia_low"]) if pd.notna(asia["asia_low"]) else None,
        "time_gate": risk.__dict__,
        "caveats": [
            "Historical snapshot, not live authorization or executable trading advice",
            "Holding duration is an example until estimated from out-of-sample trades",
            "No verified spread, order book, event calendar or CFD contract terms",
            "Session timing and Asia range are context, not demonstrated alpha",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Saba session intelligence research")
    parser.add_argument("symbol", nargs="?", default="GC=F")
    parser.add_argument("--root", default=None)
    parser.add_argument("--holding-minutes", type=float, default=20.0)
    parser.add_argument("--buffer-minutes", type=float, default=5.0)
    args = parser.parse_args(argv)
    bars = MarketArchive(args.root).read(args.symbol, "1m")
    print(json.dumps(research_snapshot(bars, holding_minutes=args.holding_minutes, buffer_minutes=args.buffer_minutes), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
