# Market Context & Signal Interpretation — Saba's advanced curriculum

## Principle
A candle pattern is a description, not an actionable forecast. Interpret each candidate in its *point-in-time* context and distinguish evidence, assumptions and uncertainty. A context label never implies a profitable trade.

## Context dimensions
1. **Market structure:** confirmed HH/HL or LH/LL, mixed/unknown, distance to *previously known* swing levels. A 1m bullish candle can occur inside a 15m downtrend.
2. **Location:** support/resistance zones known before the signal, session high/low, opening range, VWAP only if trustworthy traded volume exists. Levels are zones, not guaranteed barriers.
3. **Volatility:** current true range relative to *prior* ATR, volatility expansion/contraction, regime transitions, stale quotes and spread changes.
4. **Volume:** actual exchange volume versus tick volume; compare like-for-like sessions and contract months. Missing or ambiguous volume is not bearish, bullish or zero.
5. **Session:** time-of-day liquidity, overlapping markets, holidays, maintenance breaks, DST changes. Exchange calendar and source timestamps must be explicit.
6. **Events:** scheduled economic releases, central bank decisions and unexpected news; distinguish publication time from data reference period. Unknown event schedule means unknown risk, not a clear calendar.
7. **Execution:** bid/ask spread, depth, contract multiplier, slippage, fees, order size and latency. A chart signal with no executable edge is not a strategy.
8. **Data quality:** gaps, duplicate bars, delayed quotes, synthetic futures rolls, incorrect OHLC, partial candles. Fail closed when required context is unavailable.

## Counterexamples: identical pattern, different context
- Bullish engulfing at a previously established support zone after a controlled pullback in a confirmed bullish structure: *candidate for research*, not a buy order.
- Identical engulfing into strong prior resistance, during high spreads, or before CPI: evidence conflicts or execution is uncertain.
- Long lower wick after a downtrend can reflect rejection, a spread spike or thin liquidity; OHLC alone cannot identify cause.
- Large breakout candle on high volume can reflect continuation, absorption or exhaustion. Observe follow-through and compare against baseline, not narrative.
- Low-volatility consolidation may precede a breakout in either direction or continue sideways.
- Strong one-minute bullish candle within a bearish 15-minute regime can be a retracement, not a reversal.

## Research design
Predeclare the candidate pattern and contextual variables, exact thresholds and all timestamps. Compare pattern-only against pattern-plus-context and simple null models on identical untouched chronological holdouts. Use realistic transaction costs and matched sessions. Report calibration, effect size, uncertainty, drawdown and false positives. Test each dimension's incremental value via ablation. Do not search the holdout repeatedly to find a compelling story.

## Assessment
Saba should be able to:
- Explain why an engulfing candle is not a trading signal without location and regime.
- Construct two identical OHLC candles with contradictory market contexts.
- Distinguish 'context supports the hypothesis' from 'future price will rise'.
- Identify stale, missing and point-in-time unavailable inputs.
- Explain when 1m and 15m regimes legitimately disagree.
- Demonstrate that volume, volatility and context filters can reduce *or* increase overfitting.
- Propose a falsifiable, cost-aware, out-of-sample study.
- Recognize when 'insufficient evidence' is the only defensible conclusion.

## Implementation contract
Use `market_data/market_context.py` for transparent, conservative context assessment. The classifier is an *educational baseline*, not an alpha model. It does not place orders or assign probabilities. Its 'supported' label means consistency with explicitly supplied contextual evidence, not proof of profitability. Never treat unknown context as favorable.
