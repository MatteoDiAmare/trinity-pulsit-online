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
