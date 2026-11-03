# Forskningsnotering 2026-11-03 – BTC/USD, timstaplar, enkla regler: ingen edge under antagna kostnader

**Status:** forskningsevidens, inte ett skäl att ändra utvecklingsriktningen. Resultatet säger att tre enkla regelfamiljer på en marknad, med de kostnader som antagits nedan, inte klarar kostnaderna.
Det säger inget om andra marknader, andra regler, andra kostnader eller om intradagsskiktets värde (som är att kunna *mäta* sådant korrekt). Maskinläsbar version: `2026-11-03_btc_hourly_results.json`.

## Data
Bitstamp publika API (`/api/v2/ohlc/btcusd`, 60 min, UTC), 68 726 staplar 2019-01-01 → 2026-11-03, 100 % av förväntade timmar, inga glitchar. Parquet sha256 `4a6a43ed057930ef…`.
Utvecklingsfönster = första 80 % (2019-01-01 → 2025-04-09, dataset-hash `9f90212b09debe0a…`); de sista 20 % har aldrig använts. Forskningskopian ligger i `~/.local/share/saba-research/market_data_intraday` (mode 700),
inte i Sabas miljö; användningsrättigheterna är inte slutligt klarlagda (se `ACADEMY_INTRADAY.md`).

## Metod
Walk-forward med 180 dagars träning och 30 dagars test (70 folds, 5,75 OOS-år), parametrar valda enbart ur träningsfönstret, urvalet korrigerat med deflated Sharpe och PBO, varje körning i ett eget tomt register (försöksräkningen är alltså jämförbar).
Tre familjer × 9 celler: SMA-korsning, utbrott, mean-reversion. Varianter: Sabas intradagspolicy (inga nyinträden från 21:30, flat före 21:45 Europe/Stockholm) med normala kostnader (×1) och dubbla (×2); för SMA dessutom *uttryckligt* tillåten övernattshållning.

## Kostnadsantaganden (alla OBEKRÄFTADE mot mäklare eller handelsplats)
| Post | Värde |
|---|---|
| Avgift | 10 bps per fyllning |
| Spread | 5 bps (hel spread), ×0,9–1,2 efter UTC-timme, ×1,4 på helger |
| Slippage | 5 bps + 6 % av *föregående* stapels intervall (≈ 5 + 6–9 bps vid typisk timvolatilitet); stopp fylls med extra slippage |
| Kostnad tur och retur | ≈ 50 bps (varierar med volatilitet) |
| Finansiering | 3 bps/dygn per natt över rullning (00:00 UTC), lång och kort |
| Hävstång / risk | max 2×; 1 % av kontot riskeras per affär; dagsförlustgräns 3 %; stopp vid 20 % drawdown |
Variant ×2 dubblar spread och slippage.

## Resultat (alla verdict `no_evidence`)
| Familj | Variant | Affärer | Träff | Sharpe | Avkastning | DSR | Kostnader betalda | Över natten |
|---|---|---|---|---|---|---|---|---|
| SMA | policy ×1 | 3 912 | 0,29 | −5,8 | −99,7 % | 0,00 | 5 177 | 0 |
| SMA | policy ×2 | 3 909 | 0,24 | −9,0 | −100 % | 0,00 | 8 113 | 0 |
| SMA | övernatt tillåten ×1 | 1 190 | 0,20 | −1,6 | −81,5 % | 0,00 | 1 692 | 632 |
| SMA | övernatt tillåten ×2 | 1 264 | 0,15 | −2,9 | −95,3 % | 0,00 | 2 859 | 635 |
| Breakout | policy ×1 | 771 | 0,33 | −2,4 | −73,5 % | 0,00 | 1 216 | 0 |
| Breakout | policy ×2 | 837 | 0,29 | −3,9 | −89,8 % | 0,00 | 2 141 | 0 |
| Mean-reversion | policy ×1 | 1 800 | 0,30 | −5,6 | −97,4 % | 0,00 | 2 767 | 0 |
| Mean-reversion | policy ×2 | 2 056 | 0,24 | −9,0 | −99,8 % | 0,00 | 5 007 | 0 |
Buy&hold samma OOS-stapler: Sharpe 0,84. "Kostnader betalda" är summerade i kontovaluta över alla testfönster.

## Tolkning och gränser
- **Kostnaderna dominerar.** Med ≈ 50 bps tur och retur och hundratals affärer per år kan en enkel timregel inte betala sig; policyn (ingen övernattshållning) ger fler, kortare affärer och därmed högre kostnader än den övernattstillåtna varianten. Dubbla kostnader försämrar varje resultat monotont.
- **Beror på antagandena.** Om verkliga kostnader är lägre (annan plats, makerorder) eller strategin handlar mer sällan ändras bilden; det är exakt vad kostnadskänslighetsanalysen finns för. Inget här bevisar att BTC saknar edge.
- **Statistisk styrka:** en marknad × 5,75 år × 9 försök kan bara upptäcka en sann årlig Sharpe på ≈ 3,6 (80 % styrka). Resultatet är därför ett test av *metoden* snarare än av BTC.
- **Inte genomfört:** helgdagar i kalendern, framåtföljning av intradagsplaner, flera marknader, andra intervall, kostnader mätta mot en riktig venue.
- **Policybevis:** med policyn 0 affärer över natten (mätt som lokal kalenderdatumskillnad); övernattsvarianten har 632 av 1 190 och säger `allow_overnight: true` i sin sparade spec.

## Reproduktion
`python3 integration/run_intraday_experiments.py ~/.local/share/saba-research/market_data_intraday OUT --workers 3` och `python3 integration/analyze_intraday.py OUT` (≈ 25 minuter). Kod: commit `6d6ac2de` eller senare.
