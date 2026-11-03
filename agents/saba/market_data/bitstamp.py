"""Bitstamp public OHLC client for the Academy's intraday archive (true BTC/USD, UTC).

Source: Bitstamp's documented public REST endpoint `/api/v2/ohlc/{pair}/` (no key). Their API documentation states 400 requests/second and a default
10,000 requests per 10 minutes, at most 1000 candles per request; companies that use the data commercially must sign a Data License Agreement
(see ACADEMY_INTRADAY.md - a legal question for the operator). This client is deliberately gentle (default 4 requests/second), backs off on HTTP 429/5xx,
never keeps the still-forming candle, and tags the archive metadata with its real source.

Usage (separate intraday archive; the daily archives are untouched):
  python -m market_data.bitstamp --root ~/.local/share/saba/market_data_intraday --interval 60m --start 2019-01-01
Archive symbol: BTCUSD-BITSTAMP  (instrument spec BTCUSD_BITSTAMP). Re-running appends only what is new (merge, never replace).
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pandas as pd

BASE = "https://www.bitstamp.net/api/v2/ohlc/{pair}/"
STEPS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "60m": 3600, "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400}
PAGE = 1000


def _http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "saba-academy-research/1.0 (personal research)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


class BitstampProvider:
    source = "Bitstamp public API (/api/v2/ohlc)"
    adjustment = "raw exchange OHLC (BTC/USD spot), no adjustments; zero-volume candles = no trades in that period"

    def __init__(self, pair: str = "btcusd", *, start, end=None, rate_per_s: float = 4.0, opener=None, sleep=time.sleep, now=None, retries: int = 5):
        if not pair.isalnum() or rate_per_s <= 0 or rate_per_s > 20:
            raise ValueError("pair must be alphanumeric and rate_per_s in (0, 20]; Bitstamp allows far more, this client stays gentle")
        self.pair, self.rate, self.retries = pair.lower(), rate_per_s, retries
        self._open, self._sleep = opener or _http_get, sleep
        self.start = pd.Timestamp(start, tz="UTC") if pd.Timestamp(start).tzinfo is None else pd.Timestamp(start).tz_convert("UTC")
        self.end = None if end is None else (pd.Timestamp(end, tz="UTC") if pd.Timestamp(end).tzinfo is None else pd.Timestamp(end).tz_convert("UTC"))
        self._now = now
        self.requests = 0

    def __call__(self, symbol):          # MarketArchive.ingest(symbol, interval, provider=<this>) -> provider(symbol).history(...)
        return self

    def _get(self, url):
        for k in range(self.retries):
            self._sleep(1.0 / self.rate)
            self.requests += 1
            try:
                return json.loads(self._open(url))
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504):
                    self._sleep(min(60, 2 ** (k + 1)))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                self._sleep(min(60, 2 ** (k + 1)))
        raise RuntimeError(f"Bitstamp request kept failing: {url}")

    def history(self, period=None, interval: str = "60m", auto_adjust=False, actions=True, **_):
        if interval not in STEPS:
            raise ValueError(f"interval must be one of {sorted(STEPS)}")
        step = STEPS[interval]
        now = self._now if self._now is not None else pd.Timestamp(datetime.now(timezone.utc))
        end_ts = int((self.end or now).timestamp())
        cursor, rows = int(self.start.timestamp()), {}
        while cursor <= end_ts:
            d = self._get(BASE.format(pair=self.pair) + f"?step={step}&limit={PAGE}&start={cursor}")
            ohlc = (d.get("data") or {}).get("ohlc") or []
            if not ohlc:
                break
            for r in ohlc:
                t = int(r["timestamp"])
                if t + step > int(now.timestamp()) or t > end_ts:        # the still-forming candle (or beyond the range) is never kept
                    continue
                rows[t] = (float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"]))
            last = int(ohlc[-1]["timestamp"])
            if last < cursor:                                            # no progress: stop instead of looping
                break
            cursor = last + step
            if len(ohlc) < PAGE and last + step > end_ts:
                break
        if not rows:
            raise ValueError("Bitstamp returned no candles for the requested range")
        ts = sorted(rows)
        df = pd.DataFrame([rows[t] for t in ts], columns=["Open", "High", "Low", "Close", "Volume"], index=pd.to_datetime(ts, unit="s", utc=True))
        df["Dividends"], df["Stock Splits"] = 0.0, 0.0
        return df


def main(argv=None) -> int:
    from .archive import MarketArchive
    p = argparse.ArgumentParser(description="Fetch Bitstamp BTC/USD candles into the intraday archive")
    p.add_argument("--root", required=True, help="intraday archive root (separate from the daily archives)")
    p.add_argument("--interval", default="60m", choices=sorted(STEPS))
    p.add_argument("--start", required=True, help="ISO date, e.g. 2019-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--pair", default="btcusd")
    p.add_argument("--symbol", default="BTCUSD-BITSTAMP", help="archive symbol")
    p.add_argument("--rate", type=float, default=4.0, help="requests per second (default 4; Bitstamp's own limit is 400)")
    a = p.parse_args(argv)
    prov = BitstampProvider(a.pair, start=a.start, end=a.end, rate_per_s=a.rate)
    info = MarketArchive(a.root).ingest(a.symbol, a.interval, provider=prov)
    print(json.dumps({**info, "requests": prov.requests}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
