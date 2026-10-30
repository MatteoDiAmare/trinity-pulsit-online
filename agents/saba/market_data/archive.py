"""Yahoo Finance ingestion with persistent, validated Parquet storage.

Data is informational/research-grade; timestamps always stored in UTC.
No live trading or order placement exists in this module.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo"}
SYMBOL_RE = re.compile(r"^[A-Za-z0-9^=._-]{1,40}$")
COLUMNS = ["Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits"]


def normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Reject malformed bars and convert index to sorted UTC timestamps."""
    if frame is None or frame.empty:
        raise ValueError("provider returned no observations")
    out = frame.copy()
    if isinstance(out.columns, pd.MultiIndex):
        raise ValueError("unexpected multi-index columns; fetch one ticker at a time")
    if not isinstance(out.index, pd.DatetimeIndex):
        raise ValueError("market bars require a datetime index")
    idx = pd.to_datetime(out.index, utc=True, errors="raise")
    if idx.isna().any():
        raise ValueError("market bars contain invalid timestamps")
    out.index = idx
    out.index.name = "TimestampUTC"
    for name in ("Open", "High", "Low", "Close"):
        if name not in out.columns:
            raise ValueError(f"missing price field: {name}")
        out[name] = pd.to_numeric(out[name], errors="coerce")
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    out = out.loc[(out[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]
    out = out.loc[(out["High"] >= out[["Open", "Close", "Low"]].max(axis=1)) & (out["Low"] <= out[["Open", "Close", "High"]].min(axis=1))]
    for name in ("Volume", "Dividends", "Stock Splits"):
        if name not in out:
            out[name] = 0.0
        out[name] = pd.to_numeric(out[name], errors="coerce").fillna(0.0)
    out = out[COLUMNS].sort_index()
    out = out.loc[~out.index.duplicated(keep="last")]
    if out.empty:
        raise ValueError("no valid bars after validation")
    return out


class MarketArchive:
    """Local Parquet archive; callers must mount its root as persistent storage."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.environ.get("SABA_MARKET_DATA_DIR", "~/.local/share/saba/market_data")).expanduser().resolve()

    def _paths(self, symbol: str, interval: str):
        if not SYMBOL_RE.fullmatch(symbol) or interval not in INTERVALS:
            raise ValueError("invalid Yahoo symbol or interval")
        # Case preserved in metadata; encoded symbol cannot traverse directories.
        folder = self.root / symbol / interval
        return folder / "bars.parquet", folder / "metadata.json"

    def ingest(self, symbol: str, interval: str = "1d", period: str = "10y", *, provider=None) -> dict:
        """Fetch one Yahoo ticker, merge overlapping observations and archive atomically.

        For intraday requests Yahoo limits available history; callers must refresh
        frequently to accumulate their own archive.
        """
        data_path, metadata_path = self._paths(symbol, interval)
        if provider is None:
            import yfinance as yf
            provider = yf.Ticker
        frame = normalize_frame(provider(symbol).history(period=period, interval=interval, auto_adjust=False, actions=True))
        if data_path.exists():
            previous = normalize_frame(pd.read_parquet(data_path))
            frame = normalize_frame(pd.concat([previous, frame]))
        data_path.parent.mkdir(parents=True, exist_ok=True)
        temp = data_path.with_name("bars.tmp.parquet")
        try:
            frame.to_parquet(temp)
            os.replace(temp, data_path)
        finally:
            temp.unlink(missing_ok=True)
        info = {
            "symbol": symbol, "interval": interval, "provider": "Yahoo Finance via yfinance",
            "requested_period": period, "row_count": len(frame),
            "first_timestamp_utc": frame.index[0].isoformat(),
            "last_timestamp_utc": frame.index[-1].isoformat(),
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "adjustment": "raw OHLC, separate dividends and splits where available",
        }
        tmp_info = metadata_path.with_name("metadata.tmp.json")
        try:
            tmp_info.write_text(json.dumps(info, indent=2), encoding="utf-8")
            os.replace(tmp_info, metadata_path)
        finally:
            tmp_info.unlink(missing_ok=True)
        return info

    def read(self, symbol: str, interval: str = "1d", start: str | None = None, end: str | None = None) -> pd.DataFrame:
        data_path, _ = self._paths(symbol, interval)
        if not data_path.is_file():
            raise FileNotFoundError(f"no archived data for {symbol} {interval}; run fetch first")
        frame = pd.read_parquet(data_path)
        if start:
            frame = frame.loc[frame.index >= pd.Timestamp(start).tz_localize("UTC") if pd.Timestamp(start).tzinfo is None else frame.index >= pd.Timestamp(start).tz_convert("UTC")]
        if end:
            frame = frame.loc[frame.index <= pd.Timestamp(end).tz_localize("UTC") if pd.Timestamp(end).tzinfo is None else frame.index <= pd.Timestamp(end).tz_convert("UTC")]
        return frame

    def info(self, symbol: str, interval: str = "1d") -> dict:
        _, path = self._paths(symbol, interval)
        return json.loads(path.read_text(encoding="utf-8"))
