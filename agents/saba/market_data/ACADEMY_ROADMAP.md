# Saba Academy: development plan after the research-method phase

Goal set by the operator: Saba becomes an experienced, autonomous and profitable trader - high hit rate together with high net return and well-judged risk - using free data only.
Hard limits stay: no live trading and no broker connection until protections are verified and the operator decides; stop-loss mandatory; the operator is never put into debt.

## Status of the plan
| Item | State |
|---|---|
| 1. Automated parameter search with multiple-testing correction | **built** (studies, DSR, PBO) |
| 2. Walk-forward validation over many periods | **built** |
| 3. Testing across markets | **built** for GOLD, EURUSD, SP500; more free instruments = next |
| 4. Frozen strategies tracked forward on new data | **built** (shadow account, no LLM) |
| 5. Measuring Saba's forecasting skill | **built** (forecast at freezing, Brier score); needs many candidates and weeks of data |
| 6. Objective = net expectancy and risk, hit rate as a diagnostic | partly (reported everywhere; study criteria use expectancy and Sharpe) |
| 7. Portfolio of weak signals | planned (study over several frozen strategies as one portfolio) |
| 8. Better inputs than price (free macro/carry/volatility/seasonality) | planned (free sources only: FRED, Yahoo, central banks, CFTC) |

## Next: more statistical power, for free
Measured: three markets and ~800 out-of-sample days cannot separate a Sharpe of ~0.6 from luck. Add free instruments with specs (index futures, silver, crude, major FX pairs, BTC-USD) and use deeper
history; add the free intraday windows the provider offers (hourly ~2 years, 5-minute ~60 days) with honest cost assumptions; pool across markets. Then install studies in Saba's container, extend `/academy-cycle`
(via the usual PR) and let her run studies and freeze candidates with forecasts.

## Phase: short practical missions (designed, NOT built)
A mission = instrument (e.g. BTC/USD or gold), start capital, trading period, result target. Saba chooses method and risk inside fixed limits.
* **Mission spec**: instrument, capital, period, target, hard limits (max leverage, stop-loss mandatory, max loss and drawdown that end the mission, allowed order types), venue.
* **Venues behind one interface** (`ExecutionVenue`): historical replay with a hidden period (blind missions), forward shadow/paper on live free data, later a demo account, and only after separate approval a real broker.
  Every venue passes the same risk governor and order checks; the agent can only submit intents.
* **Shared knowledge**: a mission reads the research notebook, frozen candidates and lessons; its decisions, trades, equity and risk events go to the same ledger and registry; its outcome writes evidence-linked lessons and
  forecast scores back, so missions and long-term research improve each other. A mission cannot consume a protected test window.
* **Evaluation**: target met is not the success criterion by itself; net result, drawdown, risk taken and calibration of her pre-mission forecast are all scored.

## Phase: graphical trading platform skills (designed, NOT built)
Seeing, mouse, keyboard and sound are kept in the architecture as a *venue adapter*, not as a new agent or UI:
* A separate isolated desktop executor (virtual display and virtual audio device, no secrets, no host files, network limited to the platform) driven through Trinity's existing agent tooling with screenshot/mouse/keyboard (and speech-to-text / text-to-speech for audio alerts).
* Demo accounts first. Every action is logged with a screenshot; the same risk governor sits between intent and click (position size, stop-loss presence, daily loss, kill-switch, rate limits); the operator's kill-switch stops the executor.
* The strategies, missions and research code stay identical: only the venue changes (replay -> shadow -> GUI demo -> real, each a separate decision).

## Always
Preserve all research history and the registry; reuse existing code; record results and tests before choosing the next step; no recurring schedule and no live trading without the operator's explicit decision.
