# Market structure — advanced curriculum for Saba

Market structure is an observable description of price behavior, not a reliable forecast by itself. Use completed OHLC bars, known timestamps, and predeclared definitions.

## 1. Swing points and confirmation latency
A swing high with left width L and right width R is a bar whose high is strictly greater than highs in the L bars before it and R bars after it. A swing low is defined symmetrically. A swing point is **only confirmed after R subsequent bars close**. Plotting it at its historical pivot timestamp is permissible for visualization, but acting on it before confirmation is look-ahead bias. Equal highs/lows require a documented tie policy; this curriculum uses strict extrema and ignores ties.

## 2. Trend taxonomy
- HH: confirmed swing high above previous confirmed swing high.
- LH: confirmed swing high below previous confirmed swing high.
- HL: confirmed swing low above previous confirmed swing low.
- LL: confirmed swing low below previous confirmed swing low.
- Bullish structure: HH and HL jointly present.
- Bearish structure: LH and LL jointly present.
- Mixed/unknown: insufficient or contradictory evidence. Avoid forcing a trend label.
Swings are scale-dependent: a bullish 1-minute structure can coexist with a bearish 15-minute structure.

## 3. Breaks and changes of character
- Structure break (BOS) is often defined as a completed close beyond a prior *confirmed* swing level in the direction of the prevailing structure.
- Change of character (CHoCH) is a descriptive label for a break against prior structure. Neither term has a universal definition.
- Distinguish wick penetration, close penetration and a minimum tick/ATR buffer.
- A break cannot be assessed against a swing level that was not confirmed when the break occurred.
- Repeated crosses of the same level must not be counted as independent new breaks without a reset rule.
- Breaks can fail; a 'liquidity sweep' or 'stop hunt' is not a proven causal mechanism from candles alone.

## 4. Consolidation
Consolidation describes comparatively compressed, overlapping price action. Operationalize it with measurable metrics, e.g. rolling high-low range relative to rolling ATR, declining realized volatility, overlapping candle ranges and lack of directional swing progression. Do not infer imminent breakout direction solely from compression.

## 5. Support and resistance
Support and resistance are *zones of prior interaction*, not exact physical barriers. Candidate zones can derive from confirmed swing extremes, session highs/lows, volume profiles (only with genuine volume), or repeated tests. Document clustering tolerance in ticks/ATR and avoid using future touches to draw historical zones. Test whether zones add information beyond random levels and simple volatility baselines. More touches do not automatically mean stronger levels.

## 6. Gold-specific market structure
GC, MGC, spot XAUUSD and CFDs can have distinct prices, sessions, spreads and contract specifications. Futures rolls and session gaps can produce artificial 'breaks'. Evaluate Asia/London/New York regimes separately where relevant; use correct exchange calendars and daylight-saving rules. A daily GC=F Yahoo series cannot substantiate one-minute scalping execution.

## 7. Teaching / falsification
Saba must explain why a swing high at time t cannot be confirmed until t+R, how equal highs are handled, why a wick above resistance is not identical to a close above it, how a consolidation detector can be fooled by stale prices, and why multiple timeframe labels may disagree without contradiction. Demonstrate with synthetic sequences and assess on fresh unseen data after costs.

## 8. Research requirements
For each proposed structure strategy: predeclare swing widths, level selection, breakout thresholds, entry/exit, spread/slippage, maximum loss, market/session and holdout. Record failed hypotheses. Never promote a label alone to an executable trading signal.
