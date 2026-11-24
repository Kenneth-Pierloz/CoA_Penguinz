# CLAUDE.md — Council of Achrona (team CoA_Penguinz)

Hackathon-project voor **Hack The Future 2026, challenge 113** (aangeboden door Roboest). Datum: 24/11/2026.
De gebruiker (Mateo) praat Nederlands: antwoord in het Nederlands, simpele korte zinnen, to the point.

## Deadlines (Europe/Brussels)
- 10:00 Kennisbronnen en MCP-server open
- 11:30 Checkpoint 1: draait de mini-Council op de Kennisbronnen?
- 14:30 Checkpoint 2: eerste geldige `verdict.json` + `trace.json`
- **16:00 Inleveren**: openbare repo, laatste commit vóór 16:00 telt, link via het HTF-portaal
- 16:00–18:00 Pitches (±3 min per team). Teamkey werkt tot 18:00.

## Opdracht
Bouw een Council: een zwerm AI-agenten (Raadsleden), elk met één Rol en één eigen Kennisbron. De Koning (orchestrator) leidt het debat. Samen komen ze tot een Council Verdict: **oorzaak van de Desync, herstelplan, risico's**.

- **Raadsleden**: één agent per Rol, leest alleen de eigen Kennisbron (via MCP), noemt record-ID's als bewijs.
- **Koning**: kiest wie spreekt, met welke vraag en waarom (`kies_volgende()` in `council.py`). Leest zelf geen Kennisbron. Stelt het Verdict op.
- **Verdict** (`verdict.json`): oorzaak in 3 delen (`ingreep`, `mechanisme`, `verloop`), elk met `bewering` + `bewijs[]` (ID's); geordend herstelplan (`acties`); minstens 1 `risico`.

## Spelregels (de jury controleert dit in trace.json; overtreding = max "Beginner" op Council)
1. Eén Raadslid per Rol; elk Raadslid leest alleen de Kennisbron van zijn eigen Rol.
2. De Koning leest zelf geen Kennisbron.
3. Maximaal **10 Raadslid-beurten** per run (alleen berichten van Raadsleden tellen).
4. Vóór elke Raadslid-beurt staat een `routering` in de trace: `aan`, `vraag`, `reden`.
5. **Geen inhoud uit Kennisbronnen in prompts of code** (geen record-ID's of feiten). Wat de Council weet, komt uit het debat.
6. Menselijke tussenkomst mag, maar moet in de trace staan als spreker `mens`.
7. `trace.json` is de ongewijzigde uitvoer van de run die het Verdict maakte. **Nooit achteraf bewerken.** Kleine fixes in `verdict.json` (bv. koppeltekens) mogen.
8. `verdict.json` en `trace.json` komen uit **dezelfde run** (zelfde `run_id`).

## Jurering
Verdict / Council / Presentatie, elk 0–10. Zonder Extra uitdaging max 8/10 per criterium.
LET OP: README zegt 40% / 40% / 20%, de briefing-slides zeggen 30% / 30% / 30%. Nog bij een coach te bevestigen.
- Verdict: juiste oorzaak, herstelplan, risico's, met ID's als bewijs. Extra: `voorspelling {dag, bewering, bewijs[]}` (wanneer slaat de Desync opnieuw toe), `zekerheid` (0–1) per slot.
- Council: hoe goed leidt de Koning het debat (zichtbaar in de routeringen). Extra: Koning stopt zelf (routering met `aan: null` vóór het budget op is), houdt `open_vragen` bij in de routering.
- Presentatie: pitch ±3 min, vraag: "Waarom werkt jullie Council hier beter dan één agent?" Extra: live run, eigen visualisatie of avatars.

## De zes Rollen (details in `rolkaarten.md`)
Elke Rol heeft een Belang en kan ernaast zitten. Minstens 3 Rollen nodig.
| ROL (mapnaam) | ID-prefix | Bron |
|---|---|---|
| archmage | ARC | correspondentie + leylijnmetingen |
| engineer | ENG | installatie- en netwerklogs |
| guardian_keeper | GUA | schildspecificatie + wachtrapporten |
| historian | HIS | kroniek + archiefrecords |
| oracle | ORA | visioenendagboek |
| beast_master | BEA | logboek dierenonrust |

Kennisbronnen verwijzen soms naar andere Rollen: dat zegt welke Rol nog ontbreekt.

## Commando's (Windows PowerShell, uv)
Als `uv` niet gevonden wordt: `$env:Path = "C:\Users\Mateo\.local\bin;$env:Path"` of nieuwe terminal.
```
uv sync                # installeren (uv sync --extra devui voor DevUI)
uv run viewer          # debatviewer op http://localhost:... (eigen terminal; VIEWER_PORT=8101 voor andere poort)
uv run council         # draait de Council, schrijft trace.json (live) en verdict.json (einde)
uv run validate        # toetst verdict.json + trace.json aan schema/ en spelregels
```
Zonder uv: `python -m venv .venv`, `.venv\Scripts\activate`, `pip install -e .`, dan `python viewer.py` / `council.py` / `validate.py`.
Groen bij validate = geldig, niet per se goed.

## Code-overzicht
- `council.py`: Koning + debat. `kies_volgende(beurten, leden) -> (rol, vraag, reden)` of `(None, None, reden)` om te stoppen. Nu nog een **vaste volgorde** (placeholder). `run()` doet debat, Koning-samenvatting en schrijft het **lege Verdict (TODO)**. `kies_volgende` is gewone sync code (geen async). `BEURTENBUDGET = 10` wordt door de kit afgedwongen.
- `raadsleden/<rol>.py`: één bestand per Raadslid met `ROL`, `NAAM`, `PERSPECTIEF`. Nieuw Raadslid = kopieer `oracle.py`, pas aan; doet vanzelf mee (volgorde alfabetisch op bestandsnaam). Voeg ook `MCP_<ROL>=...` toe in `.env`. Nu aanwezig: `oracle`, `beast_master`.
- `schema/verdict.json`, `schema/trace.json`: JSON-schema's. ID-patroon: `^(ARC|ENG|GUA|HIS|ORA|BEA)-[0-9]{3}$`.
- `voorbeeld/`: ingevuld voorbeeld over een verzonnen zaak (valideert bewust niet tegen het Achrona-schema).
- `voorbeelden/devui_debat.py`: MAF DevUI group chat (gebruikt bestanden uit `kennisbronnen/`, geen MCP, schrijft geen trace).
- `viewer.py` + `viewer.html`: leest alleen trace.json en verdict.json.
- Stack: Python >=3.12, `agent-framework-core`, `agent-framework-openai`, `mcp`, `python-dotenv`, `jsonschema`.

## LLM-toegang en .env
- `.env` staat in `.gitignore`. **Lees, print of commit `.env` nooit** (bevat de teamkey, $5 budget, max 60 calls/min).
- Roboest-endpoint (blok 1): `https://roboest.findingsolutions.be/v1`, OpenAI-compatibel. Modellen: `MODEL_KLEIN=achrona` (Raadsleden), `MODEL_GROOT=achrona-groot` (Koning). `REASONING` optioneel.
- Budget op? Schakel over op FreeLLMAPI (blok 2 in `.env.example`, `http://localhost:3001/v1`) of Ollama (blok 3, zonder support).
- 429 rate limit: de client wacht en probeert opnieuw (max_retries=8).
- `RONDES` geldt alleen voor de vaste volgorde. Het Beurtenbudget (10) geldt altijd.

## Kennisbronnen
- **MCP (standaard)**: `https://mcp.findingsolutions.be/<rol>/mcp`. Zet in `.env` per Rol `MCP_<ROL>=https://mcp.findingsolutions.be/<rol>/mcp`.
  Tools per server: `lees_kennisbron`, `zoek`, `zoek_betekenis`, `toon`. Elk record heeft een ID `<ROL>-<nnn>` = bewijs.
- **Bestanden (fallback)**: `https://mcp.findingsolutions.be/pack.zip`, uitpakken in de repo-root → `kennisbronnen/<rol>/` (staat in `.gitignore`). Gebruikt als `MCP_<ROL>` leeg is; dan gaan de bestanden in de prompt.
- Geen van beide = Raadslid kan niets bewijzen en gaat ID's verzinnen.

## Huidige status (24/11, ±10:15)
- Repo: `github.com/Kenneth-Pierloz/CoA_Penguinz` (teamgenoot is eigenaar). Starter kit is ingesteld, `.env` heeft een teamkey.
- `.env`: `MCP_ORACLE=` en `MCP_BEAST_MASTER=` zijn nog **leeg** en `kennisbronnen/` is nog leeg → Raadsleden hebben nu **geen Kennisbron**. Eerst invullen.
- Eerste `uv run council` startte, maar `trace.json` staat nog op `status: "bezig"` (run niet afgemaakt).
- Alleen 2 Raadsleden (oracle, beast_master). Koning = nog vaste volgorde. Verdict = nog TODO.

## Plan (volgorde van belang)
1. `.env`: MCP-regels invullen voor alle gekozen Rollen. `uv run council` laten slagen, controleer in de viewer dat elk Raadslid een toolcall deed vóór het sprak.
2. Meer Rollen toevoegen (minstens 3, liefst 4–5 binnen 10 beurten; elke beurt telt).
3. **Koning als agent in `kies_volgende()`**: LLM (`MODEL_GROOT`, zelfde endpoint) leest het debat en geeft JSON `{"rol","vraag","reden"}` terug. Let op: sync functie, model zet soms tekst rond de JSON (robuust parsen), val bij fouten terug op een regel. Instructie: wie moet een bewering toetsen, welke Rol/oorzaak-deel ontbreekt, waar spreken Raadsleden elkaar tegen, wanneer weet je genoeg (stop zelf met `None`).
4. **Verdict door de Koning laten invullen** (LLM met schema): oorzaak in 3 delen met ID's, geordend herstelplan, ≥1 risico. Deel onbekend → `bewering: null`. ID's moeten echt uit het debat komen. Vervang speciale koppeltekens (U+2011) door `-`.
5. `geschillen` in de trace vullen: waarover waren Raadsleden het oneens en hoe is het beslecht.
6. Extra uitdagingen (zekerheid, voorspelling, Koning stopt zelf, open_vragen, live run, visualisatie).
7. Voor 16:00: één definitieve run, `uv run validate` groen, commit + push `verdict.json` + `trace.json` (root, zelfde run).

## Werkafspraken voor Claude
- Maak geen inhoud uit Kennisbronnen vast in code of prompts (spelregel 5). Test met echte runs, hardcode geen feiten.
- Pas `trace.json` nooit met de hand aan. Wil je het Verdict verbeteren? Draai opnieuw.
- Draai niet onnodig `uv run council`: budget is $5 en elke run kost tokens. Test `kies_volgende` waar kan met nepdata.
- Commit alleen als de gebruiker erom vraagt. Push nooit zonder te vragen (repo is van een teamgenoot, laatste commit vóór 16:00 telt).
- Houd uitleg kort. Dit is een wedstrijd: kies de snelste weg naar een geldig, goed onderbouwd Verdict.
