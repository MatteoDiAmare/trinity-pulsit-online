#!/usr/bin/env python3
"""Generates instrument spec JSONs for the extended free-data universe and a data profile (usable history, glitch bars, gap statistics).

* Economics (costs, leverage cap, margin, hours) are conservative RESEARCH ASSUMPTIONS per asset class, written below, and are never presented as
  verified broker terms. Saba's leverage cap never exceeds the assumed broker cap.
* Data-derived values are computed on the CLEANED, USABLE history: worst overnight gap (-> gap_stress_pct, with a safety margin and a floor per
  class) and the typical daily range (-> min_stop_pct). The three original specs (GOLD, EURUSD, SP500) are not touched.
Usage: python integration/generate_instrument_specs.py --root ~/.local/share/saba/market_data_extended [--write]
"""
import argparse, json, math, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from market_data.archive import MarketArchive
from market_data.academy.quality import check_bars, clean_bars, usable_history

OUT = Path(__file__).resolve().parents[1] / "market_data" / "academy" / "instruments"
PROFILE = Path(__file__).resolve().parents[1] / "market_data" / "academy" / "data" / "profiles.json"
GAP_CAP = 25.0     # unadjusted contract rolls inflate historical maxima (e.g. soybeans 2004: old crop -> new crop); a single roll must not shrink positions to nothing
CLASS_FLOOR_GAP = {"fx": 2.0, "equity_index_future": 6.0, "commodity_future": 5.0, "rates_future": 1.5, "crypto": 15.0}

# id: (symbol, name, asset_class, quote, tick, min_qty, step, fee, spread, slip, max_lev, broker_lev, maint, hours, days, tz, sessions, notes)
FUT_NOTE = "Continuous futures series from the free provider: contract-roll gaps are not adjusted. Broker terms unverified."
SPEC = {
    "NASDAQ100": ("NQ=F", "Nasdaq-100 (E-mini continuous futures proxy)", "equity_index_future", "USD", 0.25, 0.01, 0.01, 0.5, 1.0, 1.0, 5, 20, 0.03, 23, 252, "America/New_York", "Sun 18:00 - Fri 17:00 ET, daily 1h break", FUT_NOTE),
    "DOW30": ("YM=F", "Dow Jones 30 (E-mini continuous futures proxy)", "equity_index_future", "USD", 1.0, 0.01, 0.01, 0.5, 1.0, 1.0, 5, 20, 0.03, 23, 252, "America/New_York", "Sun 18:00 - Fri 17:00 ET, daily 1h break", FUT_NOTE),
    "RUSSELL2000": ("RTY=F", "Russell 2000 (E-mini continuous futures proxy)", "equity_index_future", "USD", 0.1, 0.01, 0.01, 0.5, 2.0, 1.5, 5, 20, 0.04, 23, 252, "America/New_York", "Sun 18:00 - Fri 17:00 ET, daily 1h break", FUT_NOTE + " Short free history (from 2017)."),
    "CRUDE_WTI": ("CL=F", "WTI crude oil (continuous futures proxy)", "commodity_future", "USD", 0.01, 0.01, 0.01, 1.0, 3.0, 2.0, 3, 10, 0.05, 23, 252, "America/New_York", "Sun 18:00 - Fri 17:00 ET, daily 1h break", FUT_NOTE + " Negative price on 2020-04-20 is dropped by validation (non-positive prices)."),
    "COPPER": ("HG=F", "Copper (COMEX continuous futures proxy)", "commodity_future", "USD", 0.0005, 0.01, 0.01, 1.0, 4.0, 3.0, 3, 10, 0.05, 23, 252, "America/New_York", "Sun 18:00 - Fri 17:00 ET, daily 1h break", FUT_NOTE),
    "NATGAS": ("NG=F", "Henry Hub natural gas (continuous futures proxy)", "commodity_future", "USD", 0.001, 0.01, 0.01, 1.5, 8.0, 5.0, 2, 5, 0.08, 23, 252, "America/New_York", "Sun 18:00 - Fri 17:00 ET, daily 1h break", FUT_NOTE + " Extremely volatile with frequent roll gaps; treat results with extra caution."),
    "SOYBEANS": ("ZS=F", "Soybeans (CBOT continuous futures proxy)", "commodity_future", "USD", 0.25, 0.01, 0.01, 1.0, 4.0, 3.0, 3, 10, 0.05, 18, 252, "America/Chicago", "Sun-Fri evening session plus day session, ~18 h", FUT_NOTE + " Daily price limits can prevent exits."),
    "US10Y_FUT": ("ZN=F", "US 10-year Treasury note (continuous futures proxy)", "rates_future", "USD", 0.015625, 0.01, 0.01, 0.5, 0.5, 0.5, 5, 10, 0.02, 23, 252, "America/Chicago", "Sun 17:00 - Fri 16:00 CT, daily 1h break", FUT_NOTE),
    "GBPUSD": ("GBPUSD=X", "GBP/USD spot FX", "fx", "USD", 0.00001, 1.0, 1.0, 0.0, 1.0, 0.5, 10, 30, 0.02, 24, 260, "UTC", "Sun ~22:00 UTC - Fri ~22:00 UTC, continuous", "Spot FX from the free provider has no real volume. Swap/financing not modelled. Capital assumed in the quote currency. Broker terms unverified."),
    "USDJPY": ("JPY=X", "USD/JPY spot FX", "fx", "JPY", 0.001, 1.0, 1.0, 0.0, 0.8, 0.5, 10, 30, 0.02, 24, 260, "UTC", "Sun ~22:00 UTC - Fri ~22:00 UTC, continuous", "Quote currency JPY; account-currency conversion is not modelled (capital is treated as quote-currency units). No real volume. Broker terms unverified."),
    "AUDUSD": ("AUDUSD=X", "AUD/USD spot FX", "fx", "USD", 0.00001, 1.0, 1.0, 0.0, 1.2, 0.6, 10, 30, 0.02, 24, 260, "UTC", "Sun ~22:00 UTC - Fri ~22:00 UTC, continuous", "Spot FX from the free provider has no real volume. Swap/financing not modelled. Broker terms unverified."),
    "USDCAD": ("CAD=X", "USD/CAD spot FX", "fx", "CAD", 0.00001, 1.0, 1.0, 0.0, 1.2, 0.6, 10, 30, 0.02, 24, 260, "UTC", "Sun ~22:00 UTC - Fri ~22:00 UTC, continuous", "Quote currency CAD; conversion not modelled. No real volume. Broker terms unverified."),
    "BTCUSD": ("BTC-USD", "Bitcoin / US dollar", "crypto", "USD", 0.01, 0.0001, 0.0001, 10.0, 5.0, 5.0, 2, 2, 0.25, 24, 365, "UTC", "24/7", "Trades every day, so 'daily gaps' are weekend-free; exchange outages and extreme moves are possible; assumed retail fees (0.1 %). Broker terms unverified."),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    arch = MarketArchive(a.root)
    ids = {"GOLD": "GC=F", "EURUSD": "EURUSD=X", "SP500": "ES=F", **{k: v[0] for k, v in SPEC.items()}, "SILVER": "SI=F"}
    profiles, specs = {}, {}
    for iid, sym in ids.items():
        raw = arch.read(sym, "1d")
        cls = (json.loads((OUT / f"{iid}.json").read_text())["asset_class"] if (OUT / f"{iid}.json").exists() else SPEC.get(iid, ("", "", "commodity_future"))[2])
        clean, removed = clean_bars(raw, asset_class=cls)
        use = usable_history(clean)
        q = check_bars(clean, asset_class=cls)
        prof = {"symbol": sym, "bars_downloaded": int(len(raw)), "first_bar": str(raw.index[0].date()), "last_bar": str(raw.index[-1].date()),
                "glitch_bars_removed": removed, "usable": use["usable"], "quality_status_after_cleaning": q["status"], "findings": [f["message"] for f in q["findings"]]}
        if use["usable"]:
            u = clean.loc[use["first_usable_date"]:]
            gap = (u["Open"] / u["Close"].shift(1) - 1).abs().dropna()
            rng = ((u["High"] - u["Low"]) / u["Close"]).median()
            prof.update({"usable_from": use["first_usable_date"], "bars_usable": use["bars_usable"], "flat_bar_share": use["flat_share_in_usable"],
                         "max_overnight_gap_pct": float(gap.max() * 100), "gap_p999_pct": float(np.percentile(gap, 99.9) * 100),
                         "median_daily_range_pct": float(rng * 100), "daily_vol_pct": float(u["Close"].pct_change().std() * 100)})
        else:
            prof["reason"] = use.get("reason")
        profiles[iid] = prof
        if iid in SPEC and use["usable"]:
            (_, name, cls_, quote, tick, mq, step, fee, spr, slp, lev, blev, maint, hrs, days, tz, sess, notes) = SPEC[iid]
            gap_stress = min(GAP_CAP, max(CLASS_FLOOR_GAP[cls_], math.ceil(prof["max_overnight_gap_pct"] * 1.25 * 2) / 2))
            min_stop = round(max(0.05, 0.25 * prof["median_daily_range_pct"]) / 0.05) * 0.05
            specs[iid] = {"id": iid, "symbol": sym, "name": name, "asset_class": cls_, "quote_currency": quote, "tick_size": tick, "min_quantity": mq, "quantity_step": step,
                          "costs": {"fee_bps": fee, "spread_bps": spr, "slippage_bps": slp}, "max_leverage": float(lev), "broker_max_leverage_assumed": float(blev),
                          "maintenance_margin_fraction": maint, "gap_stress_pct": float(gap_stress), "min_stop_pct": round(min_stop, 2),
                          "trading_hours_per_day": float(hrs), "trading_days_per_year": int(days), "timezone": tz, "sessions": sess,
                          "notes": notes + f" gap_stress_pct = 1.25 x the worst overnight gap {prof['max_overnight_gap_pct']:.1f}% in the cleaned usable history (floor per asset class, capped at 25 % because unadjusted contract rolls inflate the historical maximum); "
                                           f"min_stop_pct = 25 % of the median daily range {prof['median_daily_range_pct']:.2f}%."}
    for iid, p in profiles.items():
        flag = "usable" if p["usable"] else "NOT USABLE"
        print(f"{iid:12s} {flag:10s} from {p.get('usable_from','-'):10s} bars {p.get('bars_usable','-'):>5} removed {len(p['glitch_bars_removed']):2d} maxgap {p.get('max_overnight_gap_pct',0):5.1f}% range {p.get('median_daily_range_pct',0):.2f}%"
              + (f" -> spec gap_stress {specs[iid]['gap_stress_pct']} min_stop {specs[iid]['min_stop_pct']}" if iid in specs else ""))
    if a.write:
        for iid, s in specs.items():
            (OUT / f"{iid}.json").write_text(json.dumps(s, indent=2) + "\n")
        PROFILE.parent.mkdir(parents=True, exist_ok=True)
        PROFILE.write_text(json.dumps({"generated_from": "free Yahoo Finance daily history", "profiles": profiles}, indent=1, sort_keys=True) + "\n")
        print(f"wrote {len(specs)} specs and the data profile")


if __name__ == "__main__":
    main()
