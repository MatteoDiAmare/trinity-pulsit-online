# Körningsplan för godkännande – två separata driftsättningar (status 2026-11-03)

**Ingenting i denna plan är utfört.** Del A (Trinity-backend) och del B (intradagsstöd i Sabas Academy) godkänns var för sig. Inga ändringar av cykelbudget, schema eller livehandel i någon av dem.

## 0. Nuläge och säkerhetskopior (redan på plats)
| Sak | Läge |
|---|---|
| Trinity-databasen | Verifierad onlinebackup `~/.local/share/saba-audit/trinity-db-backups/trinity-db-20261103T152924Z.db` (10,5 MB, 82 tabeller, `integrity_check` ok, stabila tabeller radlika med live, transfer-checksumma ok). Återställningsrepetition i tempkopia: 17 agenter = live-API:t, dagens retentionsändring ingår. Mode 600 i katalog 700. |
| Varför Trinitys egen backup blev inaktuell | Jobbet 03:30 UTC har `misfire_grace_time=3600`; värden är i vänteläge då ("missed by 5:38:24") och körningen hoppas över. Senast lyckade egna kopia 2026-10-31. **Rättelse = Trinity PR #4 (ej mergad)** + värdens verifierade backupskript (`backup_trinity_db.py`, redo men **ej schemalagt**, se nedan). |
| Sabas data | Snapshot `~/.local/share/saba-audit/saba-install-snapshots/20261103T153704Z` (222 filer med sha256-manifest, mode 700): `market_data`, `market_data_extended`, `strategies`, kodversion `952f942c`, budget, guardrails, containrarnas starttider. |
| Regression mot hennes forskning | `saba_install_check.py reproduce` med den NYA koden mot snapshot: studie 56 identisk (alla mått, 1e-9), 35 av 35 enskilda experiment (inkl. 28 sandlådestrategier) omräknade utan avvikande mått, 35/35 instrumentspecar hashar till registrerat värde, dataset-hashar stämmer, 21/21 lärdomars bevis intakta. Detta hittade och rättade en verklig avvikelse (buy&hold-referensen i äldre poolningsläge). |
| Intradagsdata | Bitstamp-arkivet ligger **endast** i `~/.local/share/saba-research/market_data_intraday` (700) med rättighetsnotis. Ingen intradagsdata i Sabas container, ingen automatisk Yahoo-insamling. |
| Plattformens läge | 0 pågående körningar, inga Saba-cykler pågår (senaste: 9 `done`), enda aktiva schemat i flottan: `cornelius-pulsit-online` måndagar 07:00 UTC (neos två är avstängda). |

## A. Trinity-backend: `OPS_LOG_READ_AGENTS=saba` (+ PR #4) – kräver separat godkännande
**Ändring:** endast backend-imagen och en miljövariabel. PR #3 (mergad, `20958cee`) lägger till en agentbunden loggläsning för `ops`-nycklar; PR #4 gör backupjobbet tåligt mot vänteläge. Ingen databasmigrering, inga schemaändringar.
**Förutsättningar (kontrolleras direkt före):** (1) färsk verifierad backup (`backup_trinity_db.py` → ok, <1 h gammal); (2) inga pågående körningar (`GET /api/executions?status=running` = 0) och inte måndag ~07:00 UTC; (3) PR #4 godkänd/mergad eller uttryckligen utelämnad; (4) du är tillgänglig att logga in efteråt.
**Påverkan:** backend är nere ca 30–90 s. Alla JWT blir ogiltiga (du loggar in igen; arkiveraren använder admin-JWT tills nyckeln finns), MCP-klienter måste återansluta (`/mcp`). Agentcontainrar och deras MCP-nycklar berörs inte. Pågående uppgifter skulle förlora callbacks, därför tom körningslista som krav.
**Steg**
1. `backup_trinity_db.py` (färsk backup) och `saba_install_check.py snapshot` (Sabas data).
2. `cd ~/trinity-pulsit-online && git pull --ff-only origin main` (checkouten är idag `4d0bd3c4`; diffen är de filer PR #3/#4 rör, verifieras med `git diff --stat` före).
3. Lägg till `OPS_LOG_READ_AGENTS=saba` i `.env` (filen är idag `644` och innehåller hemligheter; rekommendation: `chmod 600 .env`, kräver ditt ja).
4. Rollback-tagg: `docker tag trinity-pulsit-online-backend:latest trinity-pulsit-online-backend:rollback-20261103`.
5. `docker compose build backend` (gamla containern kör under bygget; misslyckas bygget är inget ändrat).
6. `docker compose up -d --no-deps backend` (endast backend återskapas).
7. Verifiera: `/api/version`/hälsa; 17 agenter, övriga containrars starttider oförändrade; Sabas schema = 0; `GET /api/settings/retention` visar 30/30 och backup-blocket; backend-loggen visar "DB backup scheduler started"; du kör `trinity_login.sh`.
8. Därefter (egen mikro-åtgärd, du): `trinity_archive_key.sh create`; verifiera att nyckeln läser Sabas logg (200), inte en annan agents (403) och inte kan skriva (403); arkiveraren byter då till nyckeln och admin-JWT-beroendet upphör.
**Återställning (rollback)**
- *Kod:* `docker tag …:rollback-20261103 …:latest && docker compose up -d --no-deps --force-recreate backend`, ta bort variabeln ur `.env`. Tar ~1 min, ingen datapåverkan.
- *Databas (endast om något oväntat skrivit fel):* stoppa `trinity-backend` och `trinity-scheduler` (båda skriver), ta bort `-wal/-shm`, kopiera in backupfilen som `/data/trinity.db` (`docker cp` på stoppad container), starta. Repots `restore-database.sh` gör motsvarande men för GCP-topologin; lokalt görs det manuellt enligt detta stycke.
**Stoppvillkor:** bygget misslyckas, backend blir inte `healthy` inom 2 min, antal agenter ≠ 17, någon annan containers starttid ändras, eller något steg avviker från planen → avbryt och rulla tillbaka.

## B. Intradagsstöd i Sabas befintliga Academy – kräver ditt godkännande
**Omfattning:** *endast kod* i `~/.local/share/saba/academy` (samma skript som tidigare, `deploy_academy_to_container.sh`). Ingen data läggs in, ingen cron, inget i `/academy-cycle`, ingen containeromstart, ingen ändring av cykelbudget (4/dygn, 20/cykel, 1 800 s), schema (0) eller livehandel.
**Vad hon märker:** `status` listar 17 instrument (nytt: `BTCUSD_BITSTAMP`); `run_hypothesis` på det avvisas (ingen standarddata); en studie med `interval: "60m"` avvisas med "no intraday archive is installed" utan att debitera cykelbudgeten; dagsstudier och alla andra kommandon beter sig exakt som förut (bevisat ovan). `refresh_data` oförändrad.
**Förutsättningar:** (1) färsk snapshot (steg 1); (2) Saba inaktiv: inga pågående körningar/cykler (kontrolleras); (3) kodversion att installera = senaste commit på `feature/saba-foundation-bezier` efter att hela sviten körts grön lokalt (437 tester gröna vid senaste fullkörning; körs om efter slutliga ändringar).
**Steg**
1. `saba_install_check.py snapshot` → ny snapshot (föregående gäller som reserv).
2. `deploy_academy_to_container.sh agent-saba <commit>` (ersätter bara `academy/`; `venv` återanvänds; data orörd).
3. Hela testsviten i containern, frikopplad: `docker exec -d agent-saba bash -lc 'cd ~/.local/share/saba/academy && TMPDIR=/tmp …venv/bin/python -m unittest discover -s tests > ~/.tmp/tests.log'` (≈ 6 min; kräver 100 % grönt).
4. `saba_install_check.py verify <snapshot>`: 0 ändrade gamla rader i alla register/minnen/loggar, inga ändrade arkivstaplar, övriga containrar ej omstartade, schema 0, budget oförändrad, guardrails oförändrade, strategifiler oförändrade. (Nya revisionsloggrader från verifieringens egna bridge-anrop är avsiktliga.)
5. `saba_install_check.py reproduce <snapshot>` igen (nu med den installerade koden): studie 56, 35 experiment och 21 lärdomar identiska.
6. Rapport till dig med resultat.
**Återställning:** `deploy_academy_to_container.sh agent-saba 952f942c` (föregående version) – koden är det enda som ändras. Om data mot förmodan skulle ha ändrats: `docker cp` tillbaka katalogerna från snapshoten (mode 700 på värden) och kör `verify` mot snapshoten. Snapshoten är oföränderlig (manifest med sha256 kontrolleras av `verify`).
**Stoppvillkor:** något test rött i containern, `verify` rapporterar ett enda ändrat gammalt radvärde eller ändrad stapel, eller någon annan containers starttid ändras.

## C. Schemaläggning av värdbackupen – kräver ditt godkännande (nekades av säkerhetskontrollen utan det)
`backup_trinity_db_cron.sh` är klar men **inte schemalagd**. Förslag (värden sover ofta vid ordinarie tid, därför täta reservkörningar som bara agerar om senaste backup är >20 h):
`47 4 * * *` och `47 8-22/2 * * * … --if-stale 20`; samma reservmönster föreslås för arkiveraren (`17 8-22/2 * * * … --if-stale 20`). Fel ger `ALERT`, syslog och exit 1.

## D. Bitstamp-data och rättigheter
Arkivet stannar i den avgränsade forskningskatalogen tills du avgjort om avsedd användning är kommersiell (Bitstamps dokumentation kräver licens för företags kommersiella bruk; användarvillkoren kunde inte hämtas). Ingen uppdatering sker automatiskt. Ingen automatisk Yahoo-insamling finns eller planeras.
