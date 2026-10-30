# Volatility, volume and market noise — advanced curriculum for Saba

## Core distinction
Volatility describes variation in price, volume describes observed trading activity, liquidity describes ability to transact with limited price impact, and directional predictability describes whether future returns can be forecast. These are different quantities. A high-volume breakout can reverse; a low-volume breakout can continue. Never treat any single indicator as proof of institutional participation or of an edge.

## Volatility
- True range TR_t = max(H_t-L_t, |H_t-C_(t-1)|, |L_t-C_(t-1)|); ATR is an average of TR, with Wilder smoothing or simple rolling mean explicitly identified.
- Realized volatility of close-to-close log returns: rolling standard deviation; quote window and annualization assumptions. On 1-minute data, session gaps and stale quotes distort estimates.
- Normalized range = (H-L)/ATR or ATR/price; ratios are undefined if denominator is zero.
- Volatility clustering and regime shifts: characterize high/low relative to rolling *past* observations, not a future global percentile.
- Gap vs continuous move: a close-to-close jump is not evidence of continuous intrabar trading.

## Volume and participation
- Exchange-traded volume is contracts/shares actually transacted in that instrument and bar. GC and MGC volume are distinct. Continuous futures series and roll windows need careful handling.
- Tick volume is the count of price updates/ticks and is not interchangeable with executed contracts.
- Spot XAUUSD has no single centralized global trade-volume feed. Broker CFD volume is venue-specific.
- Relative volume RVOL = bar volume / mean prior comparable volumes. Compare the same minute-of-session and weekday where possible; a generic rolling average confounds the London/US session cycle.
- Volume can spike at absorption, liquidation, reversal or continuation. Candles cannot reveal aggressor-side volume delta without appropriate trade/order-flow data.
- Open interest is a position stock, not per-bar traded volume; it is usually published with delay.

## Noise vs meaningful movement
- A 'meaningful move' is always relative to a defined purpose: statistical surprise, persistence, executable edge or structural break. These are not synonyms.
- Test standardized returns (e.g. absolute return / prior volatility), range expansion, consecutive close persistence, structure confirmation, and liquidity-adjusted execution costs.
- Compare to null models (random/matched entry times, same-session volatility, shuffled signs with care for autocorrelation).
- A candle can look large while its expected return after spread and slippage is negative.
- Market efficiency varies by regime; don't claim to identify 'smart money' from candles and volume alone.

## Point-in-time integrity
Use completed bars. All rolling baselines for the current bar must use prior bars only. For trading decisions, know whether the current bar's volume is final and when it became available. Missing volume is *unknown*, never zero. Report data source, symbol, session, timezone, exchange holidays, contract rolls, quote type and whether volume is actual exchange-traded volume.

## Research protocol
Predeclare exact feature definitions, baseline periods, threshold candidates, target outcome, session stratification, spreads/fees/slippage, and failure criteria. Use chronological train/validation/final untouched test; guard against data snooping and survivorship effects. A higher pattern hit rate is not a trading result. Compare models with and without volume using identical future periods; evaluate whether volume adds incremental out-of-sample value.

## Graduate oral examination
1. Explain why ATR is not a directional indicator.
2. Contrast volume, tick volume, liquidity and open interest.
3. Why can a high-volume candle still be a failed breakout?
4. How does the same UTC minute compare across US daylight-saving transitions?
5. Explain why using a full-sample percentile for current volatility leaks future information.
6. Why is a price spike during a spread widening different from an executable opportunity?
7. Why must missing volume be unknown rather than zero?
8. Show a counterexample where high RVOL and a long green candle precede a fall.
9. What does a matched-session volume baseline improve over a naive 20-bar average?
10. Design a falsifiable test for whether volume adds incremental information to a 1-minute gold structure breakout.

Saba must be able to teach these distinctions and demonstrate counterexamples; memorization does not establish competence.
