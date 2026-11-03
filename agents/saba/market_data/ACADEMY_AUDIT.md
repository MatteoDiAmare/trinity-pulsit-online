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
`integration/archive_executions.py` hämtar avslutade körningar via Trinitys API (token via headerfil, aldrig på kommandoraden),
maskerar hemligheter och skriver `<id>.json.gz` + `manifest.jsonl`. Befintliga filer skrivs aldrig över. Kör det minst var femte dag
(t.ex. via cron på värden) så länge Trinitys retention är kort. Trinitys globala retention har **inte** ändrats.

## Rättelser
`academy_cli correct-lesson` ändrar aldrig en lärdom: originalet behålls oförändrat och markeras `superseded_by`; en ny lärdom med
samma bevis (samma experiment-id och resultat-hash, verifierat) och en tydlig hänvisning skapas. Båda finns kvar i databasen och i
händelseloggen.
