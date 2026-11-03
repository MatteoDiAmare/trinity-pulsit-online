# Academy – bredare data, kvalitetskontroller och statistisk styrka

Denna fil beskriver breddningen av Academy: fler kostnadsfria instrument, längre historik,
instrumentspecifikationer, datakvalitetskontroller och vad det tillför studiernas statistiska styrka.
`ACADEMY_ROADMAP.md` är oförändrad.

## 1. Källor och instrument
Endast gratis data (Yahoo Finance via `yfinance`, dagsstaplar, max historik). Inga betalda källor.
16 instrument (tidigare 3): GOLD, EURUSD, SP500 (oförändrade, byte-identiska specifikationer) samt
NASDAQ100, DOW30, RUSSELL2000, CRUDE_WTI, COPPER, NATGAS, SOYBEANS, US10Y_FUT, GBPUSD, USDJPY,
AUDUSD, USDCAD, BTCUSD. SILVER utesluts (10–28 % platta staplar, ca 461 användbara staplar).

## 2. Instrumentspecifikationer
Genereras av `integration/generate_instrument_specs.py`. Kostnader, hävstångstak och kontraktsdata är
**konservativa antaganden, inte verifierade mot en mäklare** och ska kontrolleras innan verklig handel.
- `gap_stress_pct` = 1,25 × största nattgap i rensad användbar historik (golv per tillgångsklass,
  tak 25 % eftersom orullade terminskontrakt annars blåser upp maxvärdet).
- `min_stop_pct` = 25 % av mediandagsintervallet. Sabas hävstång ≤ antagen mäklargräns.

## 3. Datakvalitet (`quality.py`)
- `check_bars`: status pass/warn/fail (gap, extremvärden, stale/platta staplar, volym). `fail` stoppar
  `run_recorded_study`.
- `find_bad_bars`/`clean_bars`: tar bort leverantörsglitchar (spike-och-återgång: |r|>5 %, >6× instrumentets
  egen 60-dagarsvolatilitet, återgång <30 %; felaktig öppning). Arkivet ändras aldrig – rensning sker i en vy.
  Totalt 14 staplar (mest FX); verkliga volatila dagar (S&P 2008, BTC) behålls.
- `usable_history`: per helår krävs ≥90 % av normalt antal staplar, ≤10 % platta, max lucka ≤10 dagar.
  Resultat i `data/profiles.json` (t.ex. GOLD från 2009, EURUSD 2004, SP500 2001, USDJPY 1997).

## 4. Metodändringar och jämförbarhet
- Folds på gemensam kalender (`fold_mode="dates"`): tidigare index-folds kunde läcka tid mellan marknader
  med olika start. Gamla 3-marknadsstudier påverkades endast av några dagars förskjutning.
- Poolning `active` (marknader ingår bara i folds där historiken täcker fönstren).
- Tidigare försökstal räknas som max per marknad (tidigare summa → överstraffade).
- `dev_end` = 80 % av det analyserade fönstret.
- **Bakåtkompatibilitet:** `fold_mode:"bars", pooling:"fixed", clean_data:false, respect_usable_from:false`
  på det gamla arkivet reproducerar de tre tidigare publicerade studierna exakt (SMA −0,155/153 affärer/DSR 0,1755;
  breakout −0,295/80/0,0358; mean-reversion −0,081/129/0,1152). Forskningshistorik (register, lärdomar,
  hypoteser) är orörd.

## 5. Resultat (samma tre familjer och rutnät, train 750 / test 125, tom registry per körning)
| Design | Marknader | OOS-år | SMA Sharpe (DSR) | Breakout | Mean-rev | Affärer SMA |
|---|---|---|---|---|---|---|
| A: gammal 10-årshistorik, ny metod | 3 | 4,8 | −0,30 | −0,78 | +0,04 | ~169 |
| B: full användbar historik | 3 | 19,1 | +0,29 (0,63) | −0,18 | +0,08 | 360 |
| C: full historik | 16 | 24,9 | 0,00 (0,21) | −0,05 | −0,75 | 2712 |
Alla verdict: `no_evidence`. Buy&hold Sharpe 0,73/0,39/0,64. I C är endast 6 av 16 marknader positiva (SMA).

## 6. Statistisk styrka
Effektivt antal oberoende marknader (buy&hold-korrelation): 3 → ≈2,4; 16 → ≈6,5 (aktieindex- och FX-kluster).
Minsta detekterbara sanna Sharpe per marknad (α 5 %, styrka 80 %, 9 försök): A 1,80 · B 0,57 · C 0,28.
Alltså ≈6× mindre detekterbar edge (≈14× fler oberoende marknadsår). Konfidensintervallet för förväntad
vinst per affär krymper 4–8×.

## 7. Begränsningar
- Orullade terminskontrakt (roll-gap) och konstanta kostnader över epoker.
- Specifikationernas ekonomi är ej mäklarverifierad.
- Effektivt N bara ≈6,5; ingen intradagsdata, makro- eller carrydata ännu.
- Ingen edge hittad i enkla dagsregler – metoden kan nu upptäcka mycket mindre edge, och
  falskpositiv-kontrollen (0/40 brus-kandidater) gäller fortfarande.
- Ej installerat i Sabas container; sker först efter verifierad testcykel och efter godkännande.

Reproduktion: `integration/run_breadth_experiments.py`, `integration/analyze_breadth.py`.

## 8. Installation i Sabas miljö (2026-11-03)
- Kod: commit `952f942c` driftsatt med `integration/deploy_academy_to_container.sh` (endast `~/.local/share/saba`, ingen omstart av containern).
- Data: det långa historikarkivet kopierades som ett **nytt** arkiv `market_data_extended` (16 instrument, sha256 över alla parquet-filer
  identiskt med det arkiv som gav de redovisade resultaten). Standardarkivet fick endast de 13 nya symbolerna; GC=F, EURUSD=X och ES=F
  rördes inte. Studier läser det utökade arkivet; `run_hypothesis`, slutprov och framåtföljning läser standardarkivet, så tidigare
  experiment, delningar och frysta kandidater reproduceras exakt. Det utökade arkivet är en överordnad version av standardarkivet för de tre
  ursprungliga (alla gemensamma stängningskurser identiska).
- Underhåll: `refresh_data` lägger till avslutade dagsstaplar (dagens ofullständiga stapel utelämnas) och redovisar staplar som
  leverantören skickat men som valideringen avvisat (t.ex. Low över Close). Gratis FX-data har sådana enstaka staplar; de lämnar ett
  endagsgap som kvalitetskontrollen tolererar.
- Kvalitet i containern: alla 16 instrument klarar kontrollen (pass/warn, inga fail).
- Historik: före/efter-jämförelse rad för rad av register, spec-index, hypoteser, länkar, cykler, frysta, uppföljningslogg och händelselogg:
  0 ändrade rader. Enda ändringen är avsiktlig: L20 är markerad som ersatt av L21 (verdict `inconclusive`, identiskt bevis).
- Körtid: en studie över 16 marknader tar ca 4–5 minuter (3 marknader ca 1 minut). Verktygsanropet måste därför ha `timeout` 600000.
