# Academy – kostnadsfri intradagsdata: kartläggning och genomförandeplan

Status 2026-11-03: **teknisk kartläggning och plan.** Ingen intradagsdata är installerad i Sabas miljö och ingen kod är ändrad. Mätningarna nedan gjordes mot
live-källorna den 3 november 2026 och mot Academys befintliga kod i ett separat arbetsarkiv.

## 1. Datakällor (mätt eller läst)
| Källa | Instrument | Intervall och djup (mätt) | Kvalitet / begränsningar | Villkor |
|---|---|---|---|---|
| **Yahoo / yfinance** (används redan) | GC=F, BTC-USD, EURUSD=X m.fl. | 1 m ≈ 8 dygn · 5 m och 15 m ≈ 60 dygn · 60 m ≈ 2 år (730 d) | Inga felaktiga OHLC-staplar. **FX har alltid volym 0**; BTC-USD har 51 % nollvolymstaplar (volymen är oanvändbar); EURUSD 9 % oförändrade stängningar. Guld är *terminskontrakt* (rullgap ej justerade). Inofficiell, strypning vid tung användning. | "endast personligt bruk"; ej godkänd av Yahoo |
| **Bitstamp** publikt REST | **BTC/USD (äkta USD)** | 1 m–3 d; timdata sedan 2012, 1 m verifierat 2019; 1000 staplar/anrop | Börsdata, UTC, ingen nyckel; tidiga år med många nollvolymstaplar | 10 000 anrop/10 min. Kommersiell användning av företag kräver licens (DLA) |
| Coinbase Exchange publikt | BTC/USD | 300 staplar/anrop, ca 10 anrop/s | Bra för korskontroll av Bitstamp | villkor ej granskade |
| Binance Vision (arkivfiler) | BTCUSDT (USDT, ej USD) | 1 m sedan 2017-08, månadsfiler med checksummor | Djupast och renast | **CC BY-NC-SA och förbjuder uttryckligen "live proprietary trading execution" (§4.2)** → avråds eftersom Sabas mål är verklig handel |
| Dukascopy datafeed | guld, FX (bid/ask-tick) | tick sedan ca 2004–2005 (stickprov) | Verkliga bid/ask → bästa spreadunderlaget. **Strypning (HTTP 503) vid snabb följd av anrop** | **Villkoren förbjuder "scraper/robot/bot" och att bygga "database of any kind"; endast personligt icke-kommersiellt.** Kräver skriftligt medgivande → används inte automatiskt |
| HistData.com | FX + XAUUSD, 1 m | flera år (djup ej verifierat) | Bid-baserade staplar, tid i EST **utan** sommartid; manuell nedladdning via formulär | inga uttryckliga villkor utöver "på egen risk"; automatisering oklar (FTP är betaltjänst) |
| OANDA v20 (demo-konto) | FX, XAU_USD | M1+ med **bid/ask/mid**, 5000 staplar/anrop (läst, ej mätt) | Kräver kontoregistrering och API-token; djup och villkor **ej verifierade** | kontovillkor, ej granskade |
| Alpha Vantage / Twelve Data | – | AV: FX/krypto-intradag är premium, 25 anrop/dygn · TD gratis: 800 krediter/dygn, 8/min, djup ej angivet | | Ej aktuella som huvudkälla |
| Stooq | – | kräver numera webbläsar-JavaScript (anti-bot) | Ej programmatiskt åtkomlig | – |

**Slutsats om data.** Gratis, licensrena, djupa *guld/FX*-intradagsdata finns inte utan kompromiss. Det enda utan konto som är rent och automatiskt är Yahoo (kort djup). Därför:
1. **Börja samla nu.** Yahoos 5 m/15 m-fönster är 60 dygn och rullar; varje dag utan insamling är permanent förlorad historia. Ett eget arkiv (`MarketArchive.ingest` slår redan ihop utan att ersätta)
   växer till 1 år 5 m-data om ett år. Veckovis insamling räcker.
2. **BTC/USD:** Bitstamp ger flera års 1 m-data med äkta USD → första djupa intradagsstudien.
3. **Guld/FX djupare än 2 år:** kräver ditt beslut (OANDA-demo, manuell HistData-import eller Dukascopy-medgivande).

**Licensfråga som du måste avgöra innan verklig handel.** Villkoren skiljer personligt, icke-kommersiellt och kommersiellt bruk. Yahoo ("personligt"), Dukascopy ("personligt, icke-kommersiellt") och Bitstamp (företag → licens)
kan alla ifrågasättas om Sabas forskning ligger till grund för handel på ett kommersiellt konto (t.ex. Heidis). Detta är en juridisk bedömning, inte en teknisk; jag har inte gjort den.

## 2. Vad den befintliga koden klarar (verifierat nu)
**Fungerar:** `MarketArchive` hanterar alla intervall; `check_bars` ger rimliga fynd på timdata (GOLD pass; BTC och EURUSD warn med relevanta anmärkningar); simulatorn kör korrekt på riktiga 24/5-staplar
(guld, EURUSD), 24/7 (BTC; `stop_gap` uppstår) och på syntetiska marknader med begränsade handelstider (6,5 h/dag, nattgap; `stop_gap` uppstår); spread, avgift, slippage och obligatoriskt stopp tillämpas vid varje fyllning;
årsannualiseringen stämmer för heldygnsmarknader (5796 beräknad mot 5748 faktiska staplar/år för guld).

**Går sönder eller saknas (konkret, med orsak):**
1. **Studiemotorn kraschar på intradag.** `run_study` poolar avkastning per *datum* (`_aggregate`: `cl.get(d)` returnerar flera staplar per dag → `TypeError`). Intradag kräver tidsstämpelnyckel eller sammanvägning till dagsavkastning.
2. **Foldlängd i staplar tolkas som vardagar** i datumfoldläget (3000 timstaplar blev 11,5 år → "utvecklingsdata för kort"). Intradag behöver `train_days/test_days`.
3. **Årsannualisering för marknader med begränsade tider:** `bars_per_year` utgår från instrumentets `trading_hours_per_day` (S&P-terminen 23 h). På en RTH-serie (7 staplar/dag) ger det 5796 i stället för 1764
   och blåser upp Sharpe ca 1,8×. Måste mätas från data (median per helår).
4. **Ingen sessionskalender.** `sessions` är fri text. Kvalitetskontrollen kan inte skilja planerad stängning (helg, daglig paus, nattstängning) från datagap, och inget hindrar nyinträden strax före stängning.
5. **Ingen intradagsdisciplin.** I RTH-testet höll 150 av 162 affärer över natten. Sabas egen `intraday_policy.py` kräver nedstängning före 21:45 (Stockholm); den tillämpas inte i simuleringen. **Alla hittills publicerade dagsstudier förutsätter övernattshållning,
   vilket skiljer sig från den policy som ska gälla vid verklig handel.**
6. **Kostnadsmodellen är platt och saknar finansiering.** Spread/slippage är konstanta bps. Intradag dominerar kostnaderna och varierar kraftigt över dygnet (tunt vid rullning/Asien). Övernattsfinansiering/swap för hävstångspositioner saknas (gäller även dagsstudierna).
7. **Dagsgränser** (daglig förlustgräns, `risk_flatten_daily`) behöver en definierad dagsgräns per marknad (UTC/handelsdag).

## 3. Trovärdig kostnadsmodell (utan bid/ask-historik)
Ingen gratis, licensren källa ger historiska bid/ask för guld/FX. Därför en modell med uttalade antaganden och robusthetskrav i stället för skenbar precision:
1. **Tidsprofil för spread** per instrument (multiplikator per UTC-timme, t.ex. guld ×3 vid rullningstimmen, ×1,5 i Asien) med antaganden som flaggas "ej verifierade mot mäklare".
2. **Volatilitetsskalad slippage** (andel av stapelns intervall/ATR) i stället för fast bps; stoppar fylls med extra slippage (finns delvis redan).
3. **Finansiering/rullning** för positioner över sessionsgräns (konfigurerbar per instrument; noll för intradagsnedstängda strategier).
4. **Stresskrav i kandidatkriteriet:** en intradagskandidat måste vara positiv även med spread/slippage ×2 (befintliga `cost_multiplier` stödjer det).
5. **Kontroll mot data:** Corwin–Schultz-skattning av spread från high/low som rimlighetskoll; för BTC/USD dessutom en liten insamlare av riktiga bid/ask från Bitstamps publika ticker (tillåtet API), som kalibrerar kryptokostnaderna över tid.

## 4. Avgränsad genomförandeplan (återanvänder arkiv, kvalitetskontroll, specar, simulator, walk-forward)
**Steg 0 – beslut (ingen kod):** (a) tillåt veckovis värdinsamling av Yahoo 60 m/15 m/5 m för GOLD, BTCUSD, EURUSD, GBPUSD, USDJPY och SP500 (RTH-fall) till ett *nytt* arkiv `market_data_intraday`; (b) licensbedömning; (c) val för djupare guld/FX.
**Steg 1 – dataskikt:** `completed_bars_provider` återanvänds; intradagsarkiv skilt från standard/utökat (gamla resultat orörda); nytt Bitstamp-hämtningsskript (paginering, 1000/anrop, hastighetsbegränsat); kvalitet: sessionskalender per instrument (öppettider som data i specen, valfritt fält med standard så gamla specar förblir byte-identiska), förväntat antal staplar, schemalagda stängningar mot datagap.
**Steg 2 – studiemotor:** tidsstämpelbaserad poolning, `train_days/test_days/step_days`, empiriskt `bars_per_year`; `SessionGuard` (sista inträde/nedstängning, återanvänder strategigränssnittet och `intraday_policy`); regressionstester med syntetisk 24/7-, 24/5- och RTH-data samt att de gamla dagsstudierna reproducerar exakt (`fold_mode="bars"` oförändrat).
**Steg 3 – kostnader:** tidsprofil, volatilitetsskalad slippage, finansiering, ×2-stresskrav; dokumenterade antaganden.
**Steg 4 – första förregistrerade intradagsstudie:** BTC (Bitstamp, flera år) och guld/FX (Yahoo, 2 år), kvalitetsgrind, effektanalys (minsta detekterbara Sharpe) före körning; ärlig förväntan: två års timdata ger för lite för att upptäcka annat än stora effekter, så värdet ligger i att bygga upp djupet.
**Oförändrat:** forskningshistorik, riskgränser, cykelbudget, avstängd livehandel. Portföljstrategier och praktiska uppdrag byggs inte.

**Uppskattad omfattning:** steg 1–3 är ca 4 mindre kodändringar med tester (inga nya tunga beroenden); steg 4 en studiekörning. Inget installeras i Sabas container före din granskning.
