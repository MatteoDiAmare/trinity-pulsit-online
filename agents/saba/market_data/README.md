# Saba Market Data v0.1

Historical research archive using Yahoo Finance via unofficial `yfinance` access.
This is **not** a real-time execution feed, a signal service, or a deployed Trinity integration.

## Setup in the Saba development WSL environment

```bash
cd /mnt/c/Users/Matteo/saba-test
source ~/saba-venv/bin/activate
pip install -r agents/saba/requirements-market-data.txt
cd agents/saba
python -m unittest discover -s tests -v
python -m market_data.cli fetch 'GC=F' --interval 1d --period 10y
python -m market_data.cli info 'GC=F'
python -m market_data.cli query 'GC=F' --tail 5
```

For the short-history intraday feed (subject to Yahoo's limits):

```bash
python -m market_data.cli fetch 'GC=F' --interval 1m --period 7d
```

Data lives under `~/.local/share/saba/market_data` unless `SABA_MARKET_DATA_DIR` or `--root` overrides it. **Mount this directory as a persistent volume for a Docker-hosted agent.** Data is not committed to Git.

To collect intraday snapshots periodically, use cron on the WSL host (ensure cron is running and adjust the checkout path if needed):

```cron
*/15 * * * * cd /mnt/c/Users/Matteo/saba-test/agents/saba && /home/matteo/saba-venv/bin/python -m market_data.cli fetch 'GC=F' --interval 1m --period 7d >> /tmp/saba-market-fetch.log 2>&1
```

Yahoo intraday history is limited; fetches may be delayed, throttled or unavailable. Deduplicate by UTC timestamp. Stored OHLC values are unadjusted; account for contract rolls, spreads, financing, slippage and broker pricing separately for derivates. Follow the provider's usage terms.

## Architecture

- `market_data.archive.MarketArchive.ingest(symbol, interval, period)`: fetch + validate + persist/merge.
- `MarketArchive.read(symbol, interval, start, end)`: pandas OHLCV frame.
- `MarketArchive.info(symbol, interval)`: source, coverage and update timestamp.
- `market_data.research.describe(symbol, interval, root)`: simple descriptive summary (not a profitable strategy).

Integration into the **running Trinity container** requires installing the dependencies in that container and mounting persistent data. The existing WSL virtual environment does not automatically become available inside Docker.

## Saba Academy replay engine

Deterministic historical trading simulator with cost model, stop-loss/take-profit, train/validation/final-test protocol and seeded experiments: see [ACADEMY.md](ACADEMY.md).
Run logs and private JSON export (Supabase Auth): see [ACADEMY_EXPORT.md](ACADEMY_EXPORT.md).
Autonomous research loop, memory and agent bridge: see [ACADEMY_AUTONOMY.md](ACADEMY_AUTONOMY.md).
