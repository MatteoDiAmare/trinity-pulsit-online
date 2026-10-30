# Candlesticks and price action — Saba's graduate-level reference

## Scope and epistemology
A candlestick is a visualization of **four observed prices in a specified interval**: open, high, low, close (OHLC). It is not a causal explanation or a trading signal by itself. Candlestick names are conventions, not universally standardized mathematical definitions. Different venues, session boundaries, aggregation rules and quote/trade feeds can produce different candles. Never claim a candle reveals the chronological path of trades within its interval: OHLC alone cannot show whether high or low happened first.

## 1. Formal language
For a valid bar with H >= max(O,C), L <= min(O,C):
- range R = H - L
- real body B = abs(C - O)
- upper wick U = H - max(O,C)
- lower wick D = min(O,C) - L
- signed body C - O
- body fraction B/R, upper fraction U/R, lower fraction D/R, if R > 0
- close location value CLV = (2C - H - L)/R, if R > 0, in [-1,1].
If R = 0, shape ratios and CLV are undefined, not evidence of indecision.
Green/bullish C > O; red/bearish C < O; equal C == O is neutral. Color conveys no future direction.

## 2. Pattern taxonomy — descriptions, not forecasts
- Doji: very small real body relative to total range; thresholds vary. Long-legged doji has prominent wicks.
- Hammer-shaped bar: small body near high, long lower wick. Context (preceding decline) distinguishes conventional 'hammer' from similar shapes elsewhere.
- Shooting-star-shaped bar: small body near low, long upper wick; preceding advance matters.
- Marubozu-like: large real body with small wicks; describes directional range expansion, not guaranteed continuation.
- Engulfing body: current real body spans the prior real body; specify whether body-only or full-range engulfing.
- Inside bar: current high < prior high AND low > prior low (strict definition); equality treatment must be documented.
- Outside bar: current high > prior high AND low < prior low.
- Harami: current body lies inside previous body; significance depends on context.
- Morning/evening star: three-bar formations; classical gaps are uncommon in continuous intraday futures. Do not mechanically transplant equity-session gap rules.
- Tweezer tops/bottoms: approximate repeated extrema; price tick tolerance must be specified.
- Pin bar: colloquial wick-dominant bar, not an exchange-standard pattern.
- Gap: difference between consecutive session open and prior close, which may reflect exchange closure or contract roll rather than news.

## 3. Context outranks labels
A candle should be interpreted relative to:
1. Trend: swings, higher highs/lows and lower highs/lows, trend strength.
2. Location: prior session high/low, opening range, established support/resistance, VWAP if true volume is available.
3. Volatility: ATR, realized volatility, range percentile; a 1 USD move can be huge or trivial depending on regime.
4. Participation: traded volume, not fabricated tick volume; futures volume and open interest refer to particular contracts.
5. Session: Asia, London, US overlap; daylight-saving shifts and CME maintenance breaks.
6. Event risk: CPI, payrolls, FOMC, geopolitical shocks; use actual publication timestamp, not observation period.
7. Liquidity: bid/ask spread, depth, order cancellations, slippage.
8. Instrument mechanics: GC vs MGC vs spot XAUUSD vs broker CFD; different fees, tick sizes, margin, rollover, trading hours and settlement.

A long lower wick can mean a rejection of lower prices, but it can also arise from a transient spread, thin liquidity or a data error. Never assert intent ('smart money hunted stops') without evidence.

## 4. Multi-timeframe analysis
Example for gold scalping: 15m for regime/context, 5m for structure, 1m for setup/execution. Aggregate from the same timestamped underlying series and consistent session rules. Never use an unfinished 15m candle when evaluating a completed 1m candle. A pattern on 1m is often noise; verify incremental value on future, unseen data.

## 5. Microstructure and gold-specific cautions
- Yahoo GC=F daily history is useful for education, **not** sufficient to validate minute scalping or fills.
- Continuous futures series can splice contracts; detect roll dates and do not treat synthetic roll gaps as executable price changes.
- One minute OHLC cannot establish stop/target ordering when both are touched within the same bar.
- A backtest using midpoint/last trades cannot assume execution at that price. Model bid/ask, commissions, queue position, market impact and adverse selection.
- Contract P&L depends on contract multiplier; an ounce move of 1 USD is not 1 USD per GC or MGC contract.
- Tick data and quotes from the intended venue/broker are preferable for realistic scalping studies.

## 6. Scientific evaluation and falsification
For each pattern, write a preregistered hypothesis:
- precise OHLC definition and all thresholds
- market/instrument, timeframe, session and date range
- entry/exit/stop, time-in-market, and transaction costs
- competing null/baseline: unconditional returns, matched random entries, momentum/mean reversion
- target outcome, effect size and uncertainty (bootstrap by day/session rather than naive independent candles)
- train/validation/final untouched test and then forward paper observation
- robustness to parameter changes, regime shifts and multiple-testing bias
- clear failure criterion and deprecation policy.

A 60% win rate can lose money if average losses exceed wins; a 40% win rate can make money if payoffs compensate. Evaluate expected value, drawdown, tail risk and confidence intervals rather than pattern hit rate alone. A statistically significant edge may be too small to cover spreads.

## 7. Self-correction protocol
When live/paper evidence contradicts a pattern:
1. Check data timestamps, corporate/futures adjustments, quote quality and spread.
2. Separate implementation bugs from failed hypotheses.
3. Compare performance by session, volatility regime and news proximity.
4. Record failed hypotheses in the experiment registry.
5. Disable a research hypothesis as an execution candidate until fresh evidence supports it.
6. Do not 'rescue' performance by repeatedly tuning on the same holdout.
7. Never describe a visual pattern as guaranteed predictive.

## 8. Teaching questions Saba must be able to answer
1. Why can two OHLC candles with identical shape represent different intrabar paths?
2. Why does a doji not necessarily predict a reversal?
3. How can a hammer-shaped candle be bullish in one context and irrelevant in another?
4. How do you distinguish body engulfing from range engulfing?
5. What is wrong with backtesting a stop and take-profit using only one-minute OHLC when both prices fall within the bar?
6. Why is volume on a continuous futures symbol not always directly comparable across rolls?
7. How does spread turn a visually promising 1m pattern into a losing trade?
8. What does an untouched holdout protect against?
9. Why must macro announcements be joined by publication time?
10. What additional data is needed before claiming a 6 USD/hour gold-scalping edge?

Saba should be able to explain each answer, derive the OHLC mathematics, and demonstrate counterexamples using synthetic bars. Reading this document is not evidence of mastery; independent assessment is required.
