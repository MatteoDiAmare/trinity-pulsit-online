# Installation restlista – Trinity / Pulsit

Den här listan är tillfällig och ska ligga kvar tills Neo och de första agenterna är driftsatta, testade och den grundläggande agentkedjan fungerar.

## Klart hittills

- [x] WSL2/Ubuntu fungerar.
- [x] Docker Desktop och Docker Compose fungerar från WSL.
- [x] `trinity-pulsit-online` är klonat lokalt.
- [x] Trinity base image är byggd.
- [x] Trinity-stacken är startad och webb-UI fungerar.
- [x] Claude Code är installerat i samma WSL-miljö som Trinity.
- [x] Claude Pro identifieras i Claude Code.
- [x] GitHub-tokenupplägg verifierat: classic PAT kan använda `repo` scope.
- [x] `neo-pulsit-online` är förberett enligt Ability/Trinity-standard.
- [x] Neo har Ability orchestrator-skills och aktuell project-management-struktur.
- [x] Neo-scheman finns deklarerade men är avsiktligt avstängda tills manuella tester är klara.

## Kvar i first-run-installationen

- [ ] Slutför `claude setup-token` i en riktig WSL-terminal.
- [ ] Anslut Claude subscription token i Trinity.
- [ ] Lägg in och verifiera GitHub PAT i Trinity.
- [ ] Kontrollera att GitHub-tokenen kan klona privata Pulsit-repon och pusha tillbaka arbete.
- [ ] Hoppa över Gemini i first-run setup tills valideringen för nya nycklar är rättad.
- [ ] Slutför Trinity first-run setup.

## Gemini-kompatibilitet

Aktuell Trinity-dokumentation/validering utgår från äldre Gemini-nycklar som börjar med `AIza`. Den nyckel som skapats för Trinity använder det nya formatet `AQ.`.

- [ ] Uppdatera Trinity så att Gemini-nycklar med prefix `AQ.` accepteras.
- [ ] Behåll stöd för äldre `AIza...`-nycklar.
- [ ] Uppdatera dokumentationen som idag säger att nyckeln måste börja med `AIza`.
- [ ] Testa den befintliga Gemini-nyckeln igen under **Settings → Integrations** efter fixen.

## Neo – driftsättning

- [ ] Säkerställ att Trinitys GitHub-token kan klona `MatteoDiAmare/neo-pulsit-online`.
- [ ] Deploy/onboard Neo repo-first från `github:MatteoDiAmare/neo-pulsit-online@main`.
- [ ] Kör Trinity compatibility check och åtgärda eventuella HARD errors.
- [ ] Verifiera att Neo får rätt Claude-credential.
- [ ] Verifiera att Neo har GitHub-åtkomst inne i sin sandbox.
- [ ] Verifiera separat `GH_TOKEN`/behörighet för Neo om project-management-skills behöver Issues write utöver plattformens clone/push-token.
- [ ] Bekräfta att Neo kör i egen Docker-sandbox/container.
- [ ] Kör ett enkelt manuellt uppdrag och verifiera loggar, repo-write och push till GitHub.

## Orkestrering och projektledning

- [ ] Testa `/discover-agents` manuellt.
- [ ] Testa fleet sync/profilering manuellt.
- [ ] Testa Ability project management med ett litet GitHub Issue-baserat projekt.
- [ ] Verifiera labels, `pending-verification`-flöde och project steward.
- [ ] Kontrollera att Neo följer hierarchy/permissions och inte kringgår human gates.
- [ ] Låt schedules/autonomy vara avstängda tills ovanstående fungerar.
- [ ] Aktivera därefter fleet sync-schedule.
- [ ] Aktivera därefter project steward-schedule.

## Nästa agenter och gemensam struktur

- [ ] Verifiera synligheten på `pulsit-online` innan privat Canon/företagsdata läggs där.
- [ ] Granska Ability `add-canon` innan vi skapar egen Canon-struktur.
- [ ] Lägg till fler agent-repon enligt namnstandarden `<förnamn>-pulsit-online`.
- [ ] Kör discovery/profile igen när fler agenter finns.
- [ ] Testa agent-till-agent-handoff via Trinity/Ability i stället för manuellt meddelandeflöde.

## Klar-kriterium

Restlistan kan tas bort eller arkiveras först när:

1. Neo kör stabilt i Trinity.
2. Claude och GitHub credentials fungerar.
3. Gemini fungerar med den nya `AQ.`-nyckeln eller med en dokumenterad kompatibel lösning.
4. Neo kan läsa, skriva och pusha sitt repo samt arbeta med GitHub Issues.
5. Minst ett orkestreringsflöde mellan Neo och ytterligare en agent är testat.
6. Human gates, permissions och schedules är verifierade innan autonom körning slås på.
