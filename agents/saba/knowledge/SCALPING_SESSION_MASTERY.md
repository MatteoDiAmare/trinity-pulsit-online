# Saba — Gold scalping, session and multi-timeframe mastery

Purpose: research education, not a profitable strategy or permission to trade. Historical observations must be tested on the instrument actually executed (XAU/USD CFD versus GC futures are different products).

## Lessons reconstructed from earlier gold-training conversations (Nov–Dec 2025)

1. **Top-down reading**: 1h establishes broad structure (HH/HL versus LH/LL), 15m shows intermediate momentum, 5m local structure, 1m execution context. A bearish 1m candle during bullish 1h structure is not by itself a reversal.
2. **Candles are conditional**: long upper wick at previously identified resistance may represent rejection; require confirmation rather than automatically shorting. An apparent break can be a liquidity sweep followed by reclaim. Doji/engulfing have no standalone edge.
3. **RSI 6/12/24, MA20, KDJ** were teaching aids, not validated signals. A K/D cross and extreme J may suggest a question for testing *only when structure, location and costs agree*. Do not fit to visually attractive charts. RSI divergence and momentum exhaustion can persist.
4. **Trend-following long**: define a bullish higher-timeframe structure, wait for a pullback toward an ex-ante known area, then test whether lower-timeframe continuation is statistically more common than matched controls. Short is symmetric in bearish structure.
5. **Countertrend and reversal**: demand more evidence than a single wick: sweep/failure, lower high/higher low, confirmed structure change, and an independently defined invalidation level.
6. **Stops and sizing**: a stop beyond a *predefined structural invalidation* is conceptually preferable to an arbitrary fixed number of pips; position size must be reduced to fit the risk budget. A stop is not a guaranteed fill. Never widen stops, average down, or deposit money to rescue a losing position.
7. **No trade** is a positive decision in chop, unknown context, high spread, unverified news risk, missing data, or poor reward/risk.

## Session literacy: clocks matter

- London Stock Exchange cash equities open 08:00 Europe/London on regular trading days; this is **09:00 Europe/Stockholm**, not always 08:00 CET. The London OTC bullion market is not identical to an LSE auction; treat 08:00 London as a research anchor, not a universal gold opening bell.
- New York cash equities open 09:30 America/New_York; US macro releases often occur 08:30 NY local, but verify each event's actual calendar and holiday schedule.
- UK and US daylight-saving transitions occur on different dates. Never hard-code fixed UTC offsets for London/NY. Store UTC; display with IANA zoneinfo.
- Define an Asia reference range *before* London, e.g. 00:00–07:59 London-local (research convention, not market fact). Test breakout, failed breakout and no-break scenarios against same-time baselines.
- Session-level relative volume must compare *matching times of day*, not the last 20 arbitrary bars. GC futures volume is not CFD tick volume; verify contract roll and provider coverage.

## Example reasoning checklist, NOT a signal

At 08:05 London-local: (a) what was known at 07:59? (b) 1h/15m/5m confirmed structure, (c) pre-open Asia range and whether price crossed it, (d) 1m completed candle and volatility relative to same session time, (e) event calendar, spread, slippage and available margin, (f) alternate interpretation, (g) evidence that would falsify thesis. If any required information is absent, explicitly say unknown.

## Research exercises

A. Compare first 15/30/60 minutes after 08:00 London versus matching control periods, separately by year, weekday, volatility regime and DST transition. Test distribution of absolute returns, range and directional continuation; no narrative substituted for measurements.
B. Evaluate Asian-range breakout and *failed* breakout with causal entry after completed candles. Compare to shuffled timestamps matched by session, and to naive trend-following.
C. Compare 1h-aligned 1m pullback entries with countertrend entries; measure net expectancy, drawdown, holding time, tail risk and uncertainty. Use nested chronological walk-forward and untouched holdout; account for spread, commission, overnight fees and realistic stop slippage.
D. Simulate simultaneous CFD positions with aggregate margin and worst-case correlated exposure. For a small $50 training account, check broker-specific minimum lot, contract size, 1:100 leverage eligibility, margin closeout and negative-balance rules. Never assume the platform's 'standard percentage 10%' equals stop distance or per-trade risk.
E. Pre-register every hypothesis and alternative explanation. Record adverse outcomes as well as favorable ones; stop promoting strategies that fail out-of-sample.

## Autonomy boundary

Saba may inspect code, read public data, create research hypotheses, run local backtests and write reports. Any real broker account, credential, transfer or live trade requires separate explicit authorization. Independent risk governor enforces portfolio constraints outside Saba's editable workspace. Heidi audits methodology but does not have to approve every read-only experiment.

## Important limits

No tested alpha is asserted here. Yahoo 1m archive has limited historical depth, so claims about seasonality require more independent history. GC=F futures proxy differs from XAU/USD CFD quotes and executions. Historical volatility and candlestick features are descriptive. A 10 USD/hour experience on 50 USD capital is not a reliable forward return expectation.
