# Saba research protocol v0.1

## Objective
Learn which data sources add **out-of-sample, after-cost predictive value**, not which source has the most features. No real orders are authorized by this research module.

## Source evaluation workflow
1. Register a candidate source with: provider, instrument, coverage, granularity, latency, cost, license/terms, timestamp semantics, revision policy, expected hypothesis and data quality checks.
2. Establish a cheap baseline using only archived price bars.
3. Align each candidate's data by **when it was actually available to Saba**, not by its economic reference date. Lag macro releases, revised data, news, and corporate events appropriately. Do not backfill future revisions into past decisions.
4. Predeclare the strategy, feature calculation, parameter search space, and evaluation window. Preserve a final untouched holdout; if multiple hypotheses are compared on a holdout, it becomes development data.
5. Run ablation: baseline vs baseline + candidate, on identical periods with identical execution assumptions, fees, spread, slippage, financing and instrument-specific constraints.
6. Record both gains and failures, data snapshot hash, configuration, code commit, and known limitations. Do not promote a source based on a single successful backtest.
7. Confirm on a fresh time period and then paper trading before recommending any live use. Assess drawdown, tail losses, trade count, confidence intervals, sensitivity to costs and market regime.

## Current implementation
- `archive.py`: Yahoo history persisted to Parquet.
- `laboratory.py`: causal moving-average baseline, cost-aware chronological evaluation, candidate-signal comparison, SQLite experiment registry.
- `lab_cli.py`: executable baseline experiment on archived data.

The current baseline is a **teaching/reference implementation**, not a robust broker simulator. It has no contract roll adjustment, margin model, financing, stop-out or exchange-specific order book. The source-ablation API compares caller-provided signals but does not automatically discover providers or validate point-in-time availability. Those capabilities remain future work.

## First run (WSL)
```bash
cd /mnt/c/Users/Matteo/saba-test
git pull --ff-only
source ~/saba-venv/bin/activate
cd agents/saba
python -m unittest discover -s tests -v
python -m market_data.cli fetch 'GC=F' --interval 1d --period 10y
python -m market_data.lab_cli run 'GC=F' --interval 1d
python -m market_data.lab_cli recent
```

All journal data is local under `SABA_MARKET_DATA_DIR` (default `~/.local/share/saba/market_data`), not in GitHub. No Trinity container deployment or live Yahoo fetch has been verified by this commit.
