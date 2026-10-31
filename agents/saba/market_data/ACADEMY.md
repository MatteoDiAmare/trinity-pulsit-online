# Saba Academy – Replay Engine v0.1

Deterministic historical simulator on top of the existing archive. **Research only: no broker,
no real orders, no network, no AI-model calls.** Reuses `MarketArchive.read` for data and
`ExperimentRegistry` (same `experiments.sqlite3`) for results; no existing API was changed.

```
market_data/academy/
  strategies.py   decision logic only (Decision, MarketView, SMA cross, breakout, mean reversion, random control)
  execution.py    CostModel + intrabar exit resolution (pure functions)
  simulator.py    chronological replay, account, ledger (decisions, orders, trades, equity)
  evaluation.py   metrics from a SimResult
  experiments.py  train/validation/test split, seeded random search, registry integration
market_data/academy_cli.py   CLI
```

## Usage (from `agents/saba`, venv active)

```bash
python -m market_data.academy_cli train --symbol GC=F --interval 1d --capital 1000 --experiments 100 --seed 42
python -m market_data.academy_cli final-test --symbol GC=F --interval 1d --experiment-id <selected_experiment_id> --ledger-dir out/
python -m market_data.lab_cli recent        # academy records are in the same registry
python -m unittest discover -s tests -v
```

`train` runs N seeded experiments (strategy and parameters sampled from `--seed`), records each
in the registry (`academy:<strategy>`) plus a summary (`academy:train_summary`), selects the best
by **validation** Sharpe (min 5 trades, no ruin) and compares it with a distribution of seeded
random-control runs. `final-test` runs one chosen experiment on the untouched test period, once
(`academy:final_test`); a repeat needs `--force` and is then no longer an unbiased test.
`--ledger-dir` writes `trades.csv`, `decisions.csv`, `orders.csv`, `equity.csv`.

## Protocol

* Chronological split 60 % train / 20 % validation / 20 % final test (by bar count).
* Training only receives bars up to the end of validation; the test slice is neither simulated nor
  hashed in `train` (tests assert this by tampering with the test period).
* Segments are simulated with earlier bars as history (warm-up) but trade only inside the segment;
  positions are force-closed at the segment end, so segments never share open risk.
* Split indices and timestamps are stored in each experiment's configuration; `final-test`
  refuses to run if the archive history no longer matches.

## Simulation assumptions

| Topic | Rule |
|---|---|
| Timing | Decision on completed bar *t* (strategy sees bars ≤ *t*, read-only); fill at **open of bar *t+1***. A decision on the last bar is never filled. |
| Spread | `spread_bps` full spread: buys pay mid+½, sells receive mid−½. Default 3 bps. |
| Slippage | `slippage_bps` extra adverse move on market orders and stop exits; none on take-profit limits. Default 2 bps. |
| Fee | `fee_bps` of notional on every fill. Default 2 bps. (Defaults match `laboratory.py`.) |
| Positions | One position at a time, long or short, fractional quantity. Opposite signal = close + open at the same open. Same-side signal is rejected (`already_positioned`). |
| Sizing | Notional + entry fee ≤ equity × `max_exposure` × `size` (default 1.0×, i.e. no leverage). |
| Insufficient capital | Orders below `min_notional` or with equity ≤ 0 are rejected and logged (`insufficient_capital`). |
| Stop / take-profit | Percent from the actual **fill** price, checked from the entry bar onwards. |
| Gaps | If the open already crosses a stop → filled at the open (`stop_gap`), not at the stop. Take-profit crossed at open → filled at the open (`take_profit_gap`). |
| Same-bar stop and TP | OHLC cannot order them. Rule: **stop first**, trade flagged `ambiguous`, counted in metrics. |
| End of data | Open position closed at the last close with costs (`end_of_data`). |
| Ruin | Equity ≤ 0 closes the position and halts trading (`halted`). |
| Marking | Equity is marked at close mid, without hypothetical exit costs. |
| Randomness | Only `numpy.random.default_rng(seed)`; same data + seed + parameters ⇒ identical results. |

Metrics: net return %, max drawdown %, trade count, hit rate, profit factor, annualized Sharpe
(per-bar returns, rf = 0, bars/year from an interval table assuming ~23 h × 252 days), return/max-drawdown,
total fees, spread+slippage cost, ambiguous-trade count, rejected orders, exposure fraction.

## Known limitations

* **Selection bias:** best-of-N on validation overstates edge (e.g. the 1d example picked a strategy
  that lost 34 % on train). Treat the validation result as a candidate, the final test as one confirmation,
  and compare with the random-control p95. Final test is a single slice; one pass is weak evidence.
* **Little data:** the 1m archive holds ~7 days (Yahoo limit), so splits cover ~1–2 days each; 1m results
  are illustrative only. The 1d archive is ~10 years of a **continuous futures proxy** (`GC=F`), not XAU/USD CFD.
* Data is unadjusted for contract rolls; roll gaps appear as price jumps and can trigger gap exits.
* No financing/swap, margin calls, overnight rules, market hours, volume/liquidity limits, partial fills,
  latency, or order-book depth. Fractional units are assumed.
* Costs are constant bps; real spreads widen around news and illiquid hours. Stops use the stop level plus
  slippage and assume fills at the level is achievable inside the bar.
* Intrabar path is unknown beyond the stop-first rule; entry-bar stops use the full bar range after the open,
  which can be pessimistic.
* One position, one instrument, no portfolio or position pyramiding.
* Annualized Sharpe on short or sparse intraday samples is very noisy and can be extreme.
* Strategies are stateless over history (random control keeps a seeded RNG); the strategy is not called
  during warm-up bars.
* Re-fetching the archive appends new bars, which moves the fraction-based split; a recorded experiment
  keeps its own indices and `final-test` checks that history still matches.
* The registry is append-only: training runs add rows to the shared `experiments.sqlite3`.
