# Academy – revisionsspår för Sabas beslut och verktygsanrop

Sessionsfiler (`~/.claude/projects/*.jsonl`) och Trinitys exekveringsloggar är tidsbegränsade (Trinity: `execution_log_retention_days`,
för närvarande 5 dagar). Revisionsspåret får därför inte bero på dem. Det finns tre lager, de två första nya:

| Lager | Var | Innehåll | Bevaras |
|---|---|---|---|
| 1. Bridge-revisionslogg | `<data>/academy_audit.sqlite3` (Sabas beständiga volym) | varje bridge-anrop: kommando, hela begäran (hypoteser, lärdomar, prognoser, strategireferenser), svar, ok/fel, varaktighet, ev. Trinity-exekverings-id | obegränsat; append-only |
| 2. Exekveringsarkiv | värden: `~/.local/share/saba-audit/trinity-executions/` | Trinitys fulla logg per körning (Sabas resonemangstext, alla verktygsanrop och resultat), maskerad, med manifest och sha256 | obegränsat; `integration/archive_executions.py` (idempotent) |
| 3. Befintliga | register, `academy_memory`, `academy_events` | experiment, hypoteser (förregistrerade), lärdomar med bevis-hash, frysta planer, tidslinje | obegränsat (oförändrat) |

## Revisionsloggen (lager 1)
- Skrivs av bridgen själv (`academy_bridge.py`), för **alla** anrop, även fel och okända kommandon. Ändrar aldrig svaret; om loggen inte kan
  skrivas skrivs ett meddelande på stderr och anropet returnerar som vanligt.
- Append-only: SQL-triggers nekar UPDATE/DELETE. Hash-kedja: varje post hashar föregående posts hash + sitt innehåll, så borttagning eller
  ändring av en tidigare post upptäcks av `verify` även om triggers tagits bort.
- Hemligheter (tokens) maskeras innan lagring. Stora svar trunkeras (hash av hela svaret sparas).
- Verifiera/läs: `python -m market_data.academy_cli --root <data> audit [--tail N] [--export fil.jsonl]` (exit 1 om kedjan är bruten).
- Gränser: loggen visar vad som skickades till och returnerades av verktygen, inte Sabas fria resonemang. Det ligger i lager 2.
  Loggen ligger på samma volym som datat: den skyddar mot förlorade sessionsfiler och mot oavsiktliga ändringar, inte mot någon med
  skrivåtkomst som bygger om hela kedjan. Exportera den (`--export`) till en plats utanför volymen för oberoende kopia.

## Exekveringsarkivet (lager 2)
Dagligt underhållsjobb på värddatorn. Det är **inte** Sabas schemaläggning och påverkar varken cykelbudget, livehandel eller andra agenter.

**Körning.** `integration/archive_executions_cron.sh` (anropar `archive_executions.py`), via crontab:
`17 4 * * *` (huvudkörning) och `17 16 * * * … --if-stale 20` (reservkörning som bara gör något om senaste lyckade körning är äldre än 20 timmar,
t.ex. om värden var avstängd kl. 04:17). Oberoende av Sabas forskningscykler. Kräver att värddatorn/WSL är igång och att cron-tjänsten körs.

**Lagring.** `~/.local/share/saba-audit/trinity-executions/`:
- `<execution_id>.json.gz` – Trinitys fulla logg (Sabas text, verktygsanrop, resultat, kostnad) för en körning, maskerad, skriven atomiskt, skrivs aldrig över;
- `manifest.jsonl` – en rad per fil: sha256 och storlek för den okomprimerade texten; nya rader är hash-kedjade (`prev`, `entry_sha256`; den första
  kedjade raden pekar på hashen av den äldre manifesten), så borttagning eller ändring av en rad upptäcks;
- `status.json`, `ALERT` (finns bara vid fel/inaktualitet), `.lock`; körlogg `~/.local/share/saba-audit/archive.log` (de senaste ca 2000 raderna).

**Åtkomst.** Kataloger `700`, filer `600`, `umask 077`; endast ägaren (värdanvändaren) kommer åt arkivet. Innehållet lämnar aldrig värddatorn.
Arkivet ligger utanför alla git-repon och ska inte checkas in (loggarna innehåller Sabas fulla resonemang).

**Hemligheter.** Inloggningstoken läses av `trinity_api.sh` ur en headerfil (aldrig på kommandoraden) och hamnar inte i arkivet. Innan skrivning maskeras
kända tokenformat (GitHub, Anthropic, `sk-`, Slack, AWS, `trinity_mcp_`), `Bearer`, JWT, privata nycklar och `password/secret/api_key/…`-tilldelningar.
En logg där ett hemlighetsmönster ändå återstår efter maskning arkiveras inte (felas och larmas). Maskeringen kan överdriva; det är avsiktligt.

**Retention.** Obegränsad: inget raderas automatiskt (några MB per månad). Trinitys egen retention (5 dagar) är oförändrad. Ta bort gamla filer först efter att du
har bestämt det, och ta då även bort deras manifestrader – verifieringen kommer annars att larma.

**Verifiering.**
- `integration/archive_executions.py --verify` kontrollerar hela arkivet på disk: varje fils hash/storlek mot manifesten, manifestkedjan, filer som saknas
  eller inte finns i manifesten, öppna rättigheter, och skannar den lagrade texten efter hemlighetsmönster. Exit 1 vid minsta avvikelse. Varje cron-körning kör den.
- `--status` visar senaste försök/lyckade körning; exit 1 om senaste körning misslyckades, `ALERT` finns eller senaste lyckade körning är äldre än 36 timmar.
- Arkivet är komplett när Trinitys lista över avslutade körningar inte innehåller något som saknas i manifesten (körningen larmar om en logg inte kunde
  arkiveras eller om listan nådde `--limit 500`).

**Felsignaler** (så att förlorad revisionshistorik upptäcks):
1. `~/.local/share/saba-audit/trinity-executions/ALERT` skapas och finns kvar tills en körning lyckas;
2. en rad i systemloggen med taggen `saba-audit` (`journalctl -t saba-audit`);
3. wrappern avslutar med exit 1, och `status.json` visar `last_result: failed` och orsaken;
4. `--status` fångar även att jobbet inte har kört alls (inaktuell lyckad körning), t.ex. om cron eller värden ligger nere.
Kontrollera `--status` regelbundet (jag kontrollerar det vid början av varje arbetspass) och efter att Windows/WSL har startats om.

**Känd begränsning – inloggning.** Arkiveringen använder operatörens Trinity-token, som gäller i 7 dagar och ogiltigförklaras av en backend-omstart.
När den löper ut misslyckas jobbet med ett tydligt larm ("Trinity login expired") tills `integration/trinity_login.sh` körs igen. Trinity behåller loggar
i 5 dagar, så en utebliven inloggning i mer än fem dagar kan ge förlorade loggar för den perioden (de som redan arkiverats finns kvar). En dedikerad,
långlivad läsnyckel vore en plattformsändring och har inte skapats.

## Rättelser
`academy_cli correct-lesson` ändrar aldrig en lärdom: originalet behålls oförändrat och markeras `superseded_by`; en ny lärdom med
samma bevis (samma experiment-id och resultat-hash, verifierat) och en tydlig hänvisning skapas. Båda finns kvar i databasen och i
händelseloggen.

## Långlivad, begränsad läsbehörighet för arkiveraren
Status 2026-11-03: **PR öppnad, ej driftsatt.** Trinity-PR: `MatteoDiAmare/trinity-pulsit-online` #3 (`OPS_LOG_READ_AGENTS`, en agent i taget, default av). Ingen omstart eller driftsättning av Trinity har gjorts;
Trinitys autentisering och scopes är oförändrade i den körande instansen. Retention är höjd (se nedan). Arkiveraren och nyckelskriptet är klara men nyckeln är inte skapad.

**Problemet.** Arkiveraren använder operatörens admin-JWT (7 dagar, dör vid backend-omstart) och Trinity behåller loggar 5 dagar.

**Vad Trinity redan har (kodgranskat i `src/backend/dependencies.py`, `routers/mcp_keys.py`).**
- MCP-nycklar (`trinity_mcp_…`): lagras som sha256-hash, har **ingen utgångstid**, kan återkallas direkt (`POST /api/mcp/keys/{id}/revoke`), visar `last_used_at`
  och attribueras i plattformens revisionslogg (nyckel-id).
- Nyckelscope `ops` (#2323): en *smalare* maskinidentitet, bara `GET` på en fast tillåtlista (fleet-hälsa, telemetri, körningslistor …), kan inte nå admin-portar
  (de är stängda för `ops` om inte en rutt uttryckligen släpper in den), kan bara skapas av en inloggad admin (inte via en annan nyckel), och dör om ägaren
  degraderas eller stängs av.
- **Luckan:** tillåtlistan innehåller `GET /api/agents/{namn}/executions` (lista) men **inte** `GET /api/agents/{namn}/executions/{id}/log`, den rutt som ger Sabas
  fulla transkript. En `ops`-nyckel kan alltså inte hämta loggarna idag.

**Alternativ**
| # | Alternativ | Ändring i Trinity | Behörighet för nyckeln | Bedömning |
|---|---|---|---|---|
| A | Nuvarande admin-JWT | ingen | full admin, 7 dagar | det vi har; kräver inloggning varje vecka |
| B | `user`-nyckel ägd av admin | ingen | **full admin, ingen utgång** | fungerar direkt men värsta tänkbara läckage; avråds |
| C | **`ops`-nyckel + en ny tillåten GET-rutt (loggen)** | 1 rad i `_OPS_ALLOWED_ROUTES` + testuppdatering | **endast läsning**, fasta rutter, ingen utgång, återkallbar | **rekommenderas** |
| D | Höj `execution_log_retention_days` 5→30 | global inställning (ingen kod) | oförändrad | bra extra skyddsnät, löser inte inloggningen |
| E | Läs Trinitys databas direkt via Docker | ingen | allt i plattformsdatabasen | avråds (går förbi autentiseringen och rör plattformens interna data) |
| F | Förläng JWT-livslängden | global auth-inställning | full admin, längre | avråds (försvagar alla sessioner) |

**Rekommendation: C, med D som skyddsnät.**
1. *Trinity-ändring (egen branch/PR i din fork, granskas av dig före sammanslagning):* lägg till exakt en rad i `_OPS_ALLOWED_ROUTES`:
   `("GET", re.compile(r"^/api/agents/[^/]+/executions/[^/]+/log$"))`, uppdatera `tests/unit/test_2323_machine_identities.py` (rutten flyttas från "nekad" till "tillåten läsning";
   nytt test att `POST` mot den och alla andra loggrutter fortsatt nekas). Ingen ändring av JWT, scopes, retention eller andra agenter.
2. *Driftsättning:* backend måste byggas om/startas om. Det ogiltigförklarar alla JWT och kräver att MCP-klienter återansluts, så det görs i ett lugnt fönster efter ditt uttryckliga godkännande.
3. *Skapa nyckeln (du, en gång):* nytt skript `integration/trinity_archive_key.sh` (samma mönster som `trinity_login.sh`: lösenord utan eko, aldrig på kommandoraden)
   loggar in, skapar nyckeln `saba-audit-archiver` med scope `ops`, skriver headerfilen `~/.config/saba-trinity/archive.hdr` (mode 600) direkt från svaret utan att skriva ut den,
   och sparar nyckel-id (inte hemligheten) för återkallning. Nyckeln visas aldrig i terminal, logg eller git.
4. *Arkiveraren* använder `archive.hdr` i stället för admin-JWT och skiljer felen åt: 401 "nyckeln återkallad/ogiltig", 403 "tillåtlistan har ändrats", anslutningsfel "Trinity nere";
   `--status` varnar när nyckeln är äldre än 90 dagar (rotation: skapa ny, verifiera, återkalla gammal med `POST /api/mcp/keys/{id}/revoke`).
5. *Verifiering före ibruktagande:* nyckeln kan hämta logg och lista körningar; nekas på chatt, filer, start/stopp, nyckelhantering och alla `POST`; återkallad nyckel ger 401 omedelbart.

**Säkerhetskonsekvenser**
- Nyckeln bär **läsrätt till alla agenters exekveringstranskript** (rutten är inte agentbegränsad i tillåtlistan) och ops-telemetri (kostnader, scheman, flottstatus). Transkript kan innehålla känsligt
  innehåll. Det är mindre än dagens admin-JWT (som kan allt) men inte noll. Arkiveraren begär bara `saba`.
- Läckt nyckel = läsning, inte skrivning; ingen utgång, så en läcka ser ut som normal användning tills `last_used_at`/revisionsloggen granskas. Därför: filmode 600, inga kopior, rotation var 90:e dag, återkalla vid minsta misstanke.
- Nyckeln är knuten till admin-kontot: degraderas eller stängs kontot av dör nyckeln (synligt larm), vilket är önskat.
- Värdkompromiss ger fortfarande nyckeln, men inte skrivrättigheter. Rätten att skapa nyckeln kräver interaktiv inloggning, så en agent kan inte skapa den själv.
- Ändringen berör en säkerhetsgräns (tillåtlistan är avsiktligt "fail closed"): den ska vara en rad, ha test och granskas som kod innan den driftsätts.

**Alternativ utan kodändring (D ensam).** Höj `execution_log_retention_days` till 30. Då gör en missad veckoinloggning ingen skada: arkiveraren hämtar allt efterhand du loggar in igen inom 30 dagar. Kostar lite diskutrymme och är en global inställning, så den kräver ditt godkännande.


### Undersökning: kan läsningen begränsas till Sabas loggar? Ja.
`ops`-nyckeln är inte agentbunden (nyckelns `agent_name` är avsiktligt NULL så att rensningsjobben ser alla agentbundna nycklar), och en ny agentbunden nyckeltyp hade krävt ändring i tre svep.
Lösningen är i stället att göra *rutten* agentspecifik: `OPS_LOG_READ_AGENTS=saba` i backendens miljö lägger till exakt
`GET /api/agents/saba/executions/{id}/log` i `ops`-tillåtlistan (inget jokertecken över agenter; andra agenters transkript förblir stängda; GET endast; anchorat; ogiltiga namn ignoreras; läses en gång vid uppstart).
Utan variabeln är beteendet oförändrat. Testat: 77 godkända i backend-venv, inklusive den befintliga `ops`-sviten; en bredare körning har identiska fel med och utan ändringen.

### Aktivering (separat godkännande krävs för punkt 1)
1. Sätt `OPS_LOG_READ_AGENTS=saba` i backendens `.env`, bygg om/starta om backend (invalidierar JWT, MCP-klienter återansluter) – **inte gjort**.
2. Du kör `integration/trinity_archive_key.sh create` i din terminal (lösenord utan eko; nyckeln skrivs direkt till `~/.config/saba-trinity/archive.hdr`, mode 600, skrivs aldrig ut).
3. Arkiveraren väljer nyckeln automatiskt; fel som återkallad nyckel (401) och avvisad rutt (403, variabeln saknas) har egna, tydliga meddelanden; `--status` varnar efter 90 dagar.
   Testat mot en lokal låtsasserver (skapa, vägra överskrivning, vägra icke-`ops`-nyckel, aldrig skriva ut nyckeln, återkalla).

### Retention (genomfört 2026-11-03, efter kontroll)
`execution_log_retention_days` och `execution_row_retention_days` höjda 5 → 30 via Trinitys validerade, revisionsloggade `PUT /api/settings/ops/config`.
**Båda krävdes:** raderna raderas helt efter `execution_row_retention_days`, så att bara höja loggens retention hade inte hjälpt.
Kontroller före ändringen: Trinitys databas är 11 MB; fleeten genererar ca 1–2,5 MB transkript per dag, alltså högst ~75 MB vid 30 dagar (säkerhetskopior: en kopia per natt, 14 dagar); disken har 950 GB ledigt.
Åtkomstskydd: databasen ligger i en Docker-volym som bara root läser på värden, i containern är filen 644 (endast backend monterar volymen), säkerhetskopiekatalogen är 700. Längre retention betyder längre tid med känsliga transkript på samma plats, med oförändrat skydd.
Återställning: `PUT /api/settings/ops/config {"settings":{"execution_log_retention_days":"5","execution_row_retention_days":"5"}}`.
**Sidofynd (oförändrat):** Trinitys egen nattliga databassäkerhetskopia är markerad `stale: true`; senaste lyckade kopia är 2026-10-31 03:30 UTC. Det är plattformens jobb och har inte rörts.
