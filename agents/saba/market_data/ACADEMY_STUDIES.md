# Saba Academy: studies, walk-forward, multi-market testing and forward tracking

Built on the existing simulator, instrument specs, registry, trial counting, sandbox and research memory. Nothing in the stored research history
is changed. Research only: no broker, no orders.

## 1. Study = one pre-registered search, evaluated honestly (`academy/study.py`)
A study is a JSON spec: a strategy (built-in, JSON spec or your own sandboxed code), `base_params`, a **grid** (dotted paths such as `short`, `params.fast`),
the **instruments**, the interval and the walk-forward geometry (`train_bars`, `test_bars`, optional `step`, `embargo`, `anchored`).
The grid is capped at 60 cells and is never truncated silently.

* **Walk-forward (`walkforward.py`)**: rolling folds per market. For each fold the parameter set is chosen **only from the training window** (mean annualized Sharpe
  over the markets, at least `min_train_trades` trades), then run on the following unseen test window. The concatenated test windows of those choices form the
  honest out-of-sample series of the whole search procedure. Test windows never overlap.
* **Multi-market**: every cell runs on every market with that market's instrument spec (costs, leverage cap, margin, stops); markets are pooled with equal weight.
* **Protected data**: only bars before each market's development end are passed to the simulation. That end is the consumed-test frontier if one exists, otherwise 80 % of the
  history; the fresh tail stays untouched (tested by tampering with it).
* **Multiple-testing correction (`stats.py`)**: *deflated Sharpe ratio* of the WFO series with N = cells + trials already spent on those markets (largest per-market count),
  using the dispersion of the cells' Sharpes; *probability of backtest overfitting* (CSCV over the cell x time matrix); grid robustness (share of cells positive, median OOS Sharpe,
  expected best-of-N noise level); buy-and-hold on the same dates; bootstrap interval of the per-trade net result.
* **Verdict** `candidate` needs ALL of: WFO Sharpe > 0, DSR >= 0.95, expectancy CI lower bound > 0, PBO <= 0.5, >= 60 % positive folds, >= 2/3 of markets positive.
  Otherwise `no_evidence` with the failed criteria listed. A candidate is only a candidate for freezing and forward tracking.
* **Bookkeeping (`run_recorded_study`)**: recorded in the registry (`academy:study`) with code version; an identical study (same spec, same data, same code) is reused; every cell x
  market is added to the trial counts, so later experiments and studies face a higher hurdle. Broken cells (e.g. a sandbox violation) are excluded but still counted.

CLI: `python -m market_data.academy_cli study --spec study.json --workers 4`. Bridge: `run_study` (one experiment of the cycle budget; own code via `code_file`).

## 2. Frozen strategies tracked forward (`academy/forward.py`)
`freeze` stores the exact plan, each market's data cutoff, pre-registered pass rules (minimum trades and days) and **Saba's forecast**: probability that the forward expectancy
is positive, expected hit rate and expected expectancy. It is immutable. `forward-update` re-simulates every active candidate on the bars that arrived after its cutoff (a shadow
account; stateless and reproducible; open positions shown separately and excluded from statistics). Verdicts: `insufficient_data` until the rules are met, then `confirmed` /
`refuted` / `inconclusive` from a bootstrap interval. `forward-report` also scores her forecasts with the **Brier score** against a coin flip, which is the measure of whether her
judgement improves over time. No LLM and no network are needed (`--fetch` refreshes the archive from the free provider). No schedule is created by any of this.

## 3. Tests (`tests/test_academy_study.py`, 33 tests)
Deflated Sharpe against an independent formula, monotonicity in trials and sample size, PSR for N = 1; PBO about 0.5 for noise and ~0 with a planted edge; fold geometry; grid limits and
dotted paths; **no look-ahead** (tampering with bars after the development end changes nothing), **selection sees only training data** (tampering with a test window changes its OOS result but
not the choice), reproducibility and parallel = sequential; broken cells; prior trials raise the hurdle; flat folds; sandboxed cells; registry dedupe and trial counting; frontier handling;
freeze validation, forward results equal a direct simulation, earlier closed trades never change when later bars arrive; verdict rules; Brier scoring; bridge commands and cycle budget.

## 4. Measured behaviour (synthetic, 2 markets, 8 folds, ~800 OOS days; 6-cell grid)
* Pure noise: **0 candidates in 40 studies**, median DSR 0.27 (no false discoveries).
* Planted momentum (AR(1) phi = 0.15 / 0.30 / 0.45): 0 candidates in 12 studies each; median WFO Sharpe 0.23 / 0.26 / 0.63, median DSR 0.30 / 0.27 / 0.40.
  **Power is low with this little data**: a Sharpe near 0.6 over ~3 years is not statistically separable from luck after correcting for the search. More markets and more out-of-sample time
  are what raise power; looser thresholds would only buy false positives.

## 5. First real results (GOLD, EURUSD, SP500 daily, 10 folds of 125 days, 750-day training windows, 80 % development data)
| Study | cells | WFO Sharpe | return | trades | hit rate | expectancy/trade | DSR | PBO | verdict |
|---|---|---|---|---|---|---|---|---|---|
| SMA crossover | 9 | -0.16 | -4.2 % | 153 | 28 % | -0.73 | 0.18 | 0.69 | no_evidence |
| Channel breakout | 12 | -0.30 | -5.8 % | 80 | 33 % | -2.11 | 0.04 | 0.37 | no_evidence |
| Mean reversion | 9 | -0.08 | -1.5 % | 129 | 49 % | -0.37 | 0.12 | 0.59 | no_evidence |
Buy-and-hold on the same dates: Sharpe 0.92. The best single SMA cell (optimistic, chosen on the OOS itself) had OOS Sharpe 0.43 and DSR 0.68, i.e. not significant once the search is priced in.

## 6. Limits
* Low statistical power on three markets and ~2000 daily bars (section 4). Breadth (more free instruments) and more history are the lever.
* The trial hurdle counts cells and earlier experiments per market but cannot know about ideas tried outside the registry.
* Equal-weight pooling ignores correlation between markets; PBO and DSR assume the pooled return series is the unit of selection.
* Forward tracking needs weeks of daily bars for a verdict; intraday would be faster but needs deeper free intraday data than the provider offers.
* The study is not yet installed in Saba's container and the `/academy-cycle` text does not yet mention `run_study`; this is intentional until the results are reviewed.
