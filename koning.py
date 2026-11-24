"""De Koning als agent: kiest wie spreekt en stelt het Verdict op.

De Koning heeft geen tools en geen Kennisbron. Hij ziet alleen het debat (`beurten` uit de trace).
Gewone, niet-async code: council.py roept dit op in een aparte thread.
"""
import difflib
import json
import os
import random
import re
import time

from openai import OpenAI

PREFIX = {"ARC": "archmage", "ENG": "engineer", "GUA": "guardian_keeper",
          "HIS": "historian", "ORA": "oracle", "BEA": "beast_master"}
# Woorden uit een andere taal: dan is het antwoord van een zwak model half vertaald.
ANDERE_TAAL = re.compile(r"\b(the|need to|whether|because|which|und|erklären|nicht|wir)\b", re.I)
EERST = {"oracle": "historian"}  # wie pas mag spreken nadat een andere Rol sprak
# Een bewering die vooral zegt dat iets onbekend is, is geen besluit: twijfel hoort in zekerheid.
GEEN_BESLUIT = re.compile(r"niet (volledig )?(vastgesteld|bewezen|aangetoond|vast te stellen|vastgelegd|bekend)|\bonbekend\b|geen raadslid|geen bewijs|staat niet vast", re.I)
SLOTS = ("ingreep", "mechanisme", "verloop")
# geen \b vooraan: in JSON-toolresultaten staat vaak een letterlijke "\n" vlak voor het ID
ID_RE = re.compile(r"(?<![A-Z0-9])(ARC|ENG|GUA|HIS|ORA|BEA)-([0-9]{3})\b")
STREPEN = re.compile("[‐-―−](?=[0-9]{3}\\b)")
POGINGEN = 4
WACHT = 40  # seconden na een LLM-fout (rate limit)


def normaal(tekst):
    """Zet speciale koppeltekens in ID's om naar een gewoon '-'."""
    return STREPEN.sub("-", tekst or "")


def vind_ids(tekst, rol=None):
    """Alle record-ID's in een tekst, desgewenst alleen die van één Rol."""
    return {m.group(0) for m in ID_RE.finditer(normaal(tekst)) if rol is None or PREFIX[m.group(1)] == rol}


def berichten(beurten):
    return [b for b in beurten if b["soort"] == "bericht" and b["spreker"] in PREFIX.values()]


def onbeantwoord(beurten):
    """Per Rol: hoe vaak een vraag van de Koning zonder bericht bleef (technische uitval)."""
    telling, wacht = {}, None
    for b in beurten:
        if b["soort"] == "routering":
            if wacht:
                telling[wacht] = telling.get(wacht, 0) + 1
            wacht = b["aan"]
        elif b["soort"] == "bericht" and b["spreker"] == wacht:
            wacht = None
    if wacht:
        telling[wacht] = telling.get(wacht, 0) + 1
    return telling


def bekende_ids(beurten):
    """ID's die als bewijs mogen dienen: genoemd door de eigen Rol en echt gezien in zijn Kennisbron."""
    ids = set()
    for b in berichten(beurten):
        ids |= vind_ids(b["tekst"], b["spreker"]) - set(b.get("ids_niet_in_bron", []))
    return ids


def lees_json(tekst):
    """Haalt het eerste JSON-object uit een antwoord, ook als er tekst rond staat."""
    for m in re.finditer(r"\{", tekst or ""):
        try:
            data, _ = json.JSONDecoder().raw_decode(tekst[m.start():])
        except ValueError:
            continue
        if isinstance(data, dict):
            return json.loads(normaal(json.dumps(data, ensure_ascii=False)))  # ook geëscapete koppeltekens
    raise ValueError("geen JSON-object gevonden in het antwoord")


def is_tekst(x):
    return isinstance(x, str) and bool(x.strip())


def tekst_debat(beurten):
    regels = []
    for b in beurten:
        if b["soort"] == "routering":
            regels.append(f"[Koning -> {b['aan']}] {b['vraag']}" if b["aan"] else f"[Koning stopt] {b['reden']}")
        elif b["soort"] == "bericht":
            regels.append(f"[{b['spreker']}]\n{normaal(b['tekst'])}")
            if b.get("ids_niet_in_bron"):
                regels.append("(Controle: niet teruggevonden in de eigen bron, dus geen bewijs: "
                              + ", ".join(b["ids_niet_in_bron"]) + ")")
            if b.get("zonder_bron"):
                regels.append("(Controle: dit Raadslid heeft zijn Kennisbron nog niet gelezen.)")
    return "\n\n".join(regels) or "(nog niets gezegd)"


KIES = """Je bent de Koning van de Council of Achrona. Je leidt een debat over de oorzaak van de Desync.
Je hebt zelf geen Kennisbron: je weet alleen wat in het debat gezegd is. Elk Raadslid kent alleen zijn eigen Kennisbron, heeft een eigen Belang en kan zich vergissen.

Het Verdict vraagt: ingreep (wie deed wat, wanneer), mechanisme (hoe leidt dat tot de symptomen), verloop (welk patroon in de tijd), een geordend herstelplan, risico's, en een voorspelling van de dag waarop de Desync opnieuw toeslaat.

Kies elke beurt bewust:
- Kies zelf de volgorde: wie helpt de zaak nu het meest vooruit? Je hoeft de Rollen niet een voor een af te lopen; vraag gerust eerst door bij iemand die al sprak. Eén regel: de historian spreekt vóór de oracle, want het verleden komt voor de toekomst.
- Spreekt een Rol voor het eerst, vraag dan ruim genoeg: welke afwijkingen staan er in de bron (dag en tijd, de vroegste eerst, en welk patroon), wat veranderde er vlak daarvoor en door wie, wat deed die Rol zelf, en welke regels, normen of voorwaarden in de bron verklaren wanneer zo'n afwijking ontstaat. Maak de vraag eigen aan die Rol en aan wat het debat nu nodig heeft; gebruik geen vast sjabloon. Pin je niet vast op één dag die iemand noemde.
- Het mechanisme is het moeilijkst. Vraag naar de regel of voorwaarde achter een symptoom en leg die naast wat anderen over de oorzaak zeiden.
- Stel één hoofdvraag met hooguit één bijvraag, samen hoogstens 80 woorden. Dat geldt ook voor een eerste vraag: kies wat nu het belangrijkst is.
- Vraag open. Leg geen lijst met mogelijke oorzaken voor en stuur een zijspoor niet langs bij anderen. Een detail dat het Verdict niet verandert, laat je los.
- "Heeft nog niet gesproken" is geen reden. Zeg welke bewering, leemte of tegenspraak juist deze Rol kan oplossen.
- Ken je een ritme of patroon, vraag dan wat dat ritme kan bepalen: welke instelling, verhouding of regel past bij precies die tussentijd?
- Laat een bewering toetsen door de Rol wiens bron ze kan bevestigen of weerleggen. Samenvallen in de tijd is nog geen oorzaak.
- Verwijst iemand naar een andere Rol, vraag het aan die Rol. Alleen Raadsleden uit de lijst kunnen antwoorden.
- Spreken Raadsleden elkaar tegen, leg het conflict scherp voor aan wie het kan beslechten. Noem iets alleen een tegenspraak als beide Raadsleden het zelf zeiden, en leg niemand woorden in de mond.
- Vraag nooit opnieuw wat al beantwoord is, ook niet als het antwoord "dat staat niet in mijn bron" was. Vraag een Rol hooguit twee keer; een derde keer alleen om een echte tegenspraak te beslechten, en noem die dan in "tegenspraak".
- Combineer zelf: twee bronnen die los van elkaar hetzelfde zeggen, dezelfde woorden gebruiken of in elkaar passen, zijn samen sterk bewijs. Je hoeft niet te wachten tot één Raadslid alles bevestigt; geen enkele Rol ziet de bron van een ander. Blijf niet hangen in één detail.
- Hou een vraag over voor veilig herstel: welke procedure, voorwaarden, volgorde en gevaren staan er in de bron van wie daar al iets over zei of er het dichtst bij staat?
- Wie iets zegt dat tegen zijn eigen Belang ingaat, is extra geloofwaardig; wie het goed uitkomt, vraagt om toetsing.
- Werk open_vragen bij: korte vragen, hoogstens acht. Schrap alleen wat met bewijs beantwoord is, voeg nieuwe toe.
- Stop zelf (rol null) zodra ingreep, mechanisme, verloop en herstel onderbouwd zijn, of zodra een extra vraag weinig meer oplevert. Mik op hoogstens negen beurten en zie de tiende als reserve voor iets dat het Verdict echt nog verandert. Noem in de reden waarom je genoeg weet en wat open blijft.

De reden is concreet: verwijs naar wie wat zei. Antwoord in het Nederlands met alleen dit JSON-object:
{"rol": "<rol>" of null, "vraag": "<vraag>" of null, "reden": "<1-3 zinnen>", "open_vragen": ["..."], "tegenspraak": "<alleen bij een derde vraag aan dezelfde Rol, anders null>"}"""

OORDEEL = """Je bent de Koning van de Council of Achrona. Het debat is klaar. Stel het Council Verdict op, uitsluitend uit het debat. Je hebt zelf geen Kennisbron.

Regels:
- Weeg bewijs, tel geen stemmen. Een spoor dat in het debat weerlegd is, hoort niet in de oorzaak.
- Combineer de bronnen tot de best onderbouwde verklaring. Geen enkel Raadslid ziet de bron van een ander, dus "dat kan ik niet nakijken" weerlegt niets. Records uit verschillende bronnen die in tijd, inhoud of woordkeuze in elkaar passen, ondersteunen elkaar.
- Jij bent rechter: verbanden leggen is jouw taak, niet die van de Raadsleden. Elk Raadslid ziet maar een stukje en zegt dus terecht dat zijn records alleen niets bewijzen. Past een regel uit de ene bron op een feit uit de andere, en klopt de volgorde in de tijd, dan is dat verband onderbouwd, ook als niemand het uitsprak.
- Geen beter alternatief in het debat? Dan is de best passende verklaring je besluit, met een eerlijke zekerheid. Schrijf in een bewering nooit dat iets niet vastgesteld, niet bewezen of onbekend is.
- Formuleer elke bewering als een duidelijk besluit, in hoogstens vier zinnen. Twijfel zet je in zekerheid, niet in de bewering. Is iets alleen geraden, laat het dan weg.
- Schrijf niemand iets toe dat hij in het debat niet zelf zei.
- Gebruik als bewijs alleen ID's uit de gegeven lijst, en alleen als dat record de bewering echt steunt. Neem per deel het sterkste bewijs, bij voorkeur uit meerdere bronnen.
- Maak eerst de keten: van ingreep naar elk belangrijk symptoom, stap voor stap, met de regel of voorwaarde uit het debat die de stap verklaart. Gebruik die keten voor het mechanisme en neem de ID's van de regels én van de symptomen op als bewijs. Een stap zonder steun in het debat laat je weg.
- Wees precies in de oorzaak: wie deed het, wie keurde het goed, wat, welke dag; de hele keten van ingreep naar symptomen; het ritme of patroon in de tijd met dagen en getallen.
- Het verloop staat in de volgorde van de tijd en gebruikt het volledige ritme dat onafhankelijke bronnen delen, niet alleen de dagen met het opvallendste symptoom.
- Weet je een deel niet, dan bewering null en bewijs []. Gok niet.
- acties staan in uitvoeringsvolgorde en houden rekening met voorwaarden en afhankelijkheden uit het debat. Volg een herstelprocedure als een Raadslid er een noemde. Noem wie het doet, en laat het plan niet wachten op iemand of iets dat volgens het debat niet beschikbaar is.
- risicos gaan over wat bij herstel of bij niets doen kan mislopen, elk met een mitigatie.
- voorspelling: dag (geheel getal) waarop de Desync opnieuw toeslaat als niemand ingrijpt, met de berekening uit het volledige ritme. De dag ligt na de laatste dag waarover het debat records heeft. Geen basis in het debat? Dan null.
- zekerheid is 0 tot 1 en eerlijk.
- vertrouwen: hoe zeker je bent van het hele Verdict, als geheel getal van 0 tot 100, met een reden van één of twee zinnen: wat draagt het, en wat is de zwakste schakel? Het past bij de zekerheid per deel; een ketting is niet sterker dan haar zwakste schakel.
- dwaalsporen: verklaringen uit het debat die niet kloppen of geen bewijs hebben, met wie ze gaf, waarom ze afvallen en het bewijs dat ze weerlegt. Neem ze niet op als oorzaak of risico. Geen? Dan [].
- geschillen: echte tegenspraken tussen Raadsleden in dit debat, met ieders standpunt. uitkomst null en bewijs [] als het niet beslecht is. Geen tegenspraak? Dan []. "Dat staat niet in mijn bron" is geen tegenspraak. Beslecht een geschil als de records samen een antwoord geven, ook als geen Raadslid dat zelf uitsprak.

Antwoord in het Nederlands met alleen dit JSON-object:
{"keten": [{"stap": "...", "regel": "...", "bewijs": ["ID"]}],
 "samenvatting": "...",
 "oorzaak": {"ingreep": {"bewering": "...", "bewijs": ["ID"], "zekerheid": 0.0}, "mechanisme": {...}, "verloop": {...}},
 "acties": [{"actie": "...", "reden": "...", "bewijs": ["ID"]}],
 "risicos": [{"risico": "...", "mitigatie": "..."}],
 "voorspelling": {"dag": 1, "bewering": "...", "bewijs": ["ID"]} of null,
 "vertrouwen": {"score": 0, "reden": "..."},
 "dwaalsporen": [{"bewering": "...", "van": "<rol>", "waarom": "...", "bewijs": ["ID"]}],
 "geschillen": [{"onderwerp": "...", "standpunten": {"<rol>": "..."}, "uitkomst": "..." of null, "bewijs": ["ID"]}]}"""


CONTROLE = """Dit is je eerste Verdict:

{verdict}

Lees het na en geef het volledige JSON-object terug, met een extra veld "correcties": ["wat je verbeterde en waarom"] ([] als alles klopt). Verander alleen wat echt fout is:
1. Een berekening van dagen, tijden of tussentijden die niet klopt.
2. Gebeurtenissen die niet in de volgorde van de tijd staan.
3. Zinnen in het Verdict die elkaar tegenspreken, bijvoorbeeld een risico dat een weerlegd spoor weer als oorzaak noemt.
4. Een middel of feit dat een Raadslid in het debat uitdrukkelijk weerlegde of introk.
5. Een dader, goedkeurder of verklarende regel die in het debat staat en ontbreekt: voeg die toe, met bewijs.
Laat al het andere staan: beweringen, bewijs, verbanden, zekerheid, vertrouwen en de voorspelling. Schrap nooit iets omdat geen Raadslid het uitsprak of nakeek."""


class Koning:
    def __init__(self, budget, vraag_llm=None, model="MODEL_GROOT"):
        self.model = model  # naam van de omgevingsvariabele
        self.budget = budget
        self.verbruik = {"beurten": 0, "invoer": 0, "gecached": 0, "uitvoer": 0}
        self.vraag_llm = vraag_llm or self._llm  # vervangbaar, om zonder API te testen
        self._client = None
        self.correcties = []  # wat de controleronde aan het Verdict verbeterde

    def _llm(self, messages):
        if self._client is None:
            self._client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["OPENAI_API_KEY"],
                                  max_retries=8, timeout=180)
        extra = {"reasoning_effort": os.environ["REASONING"]} if os.getenv("REASONING") else {}
        r = self._client.chat.completions.create(model=os.environ[self.model], messages=messages, **extra)
        v = self.verbruik
        v["beurten"] += 1
        if r.usage:
            v["invoer"] += r.usage.prompt_tokens or 0
            v["uitvoer"] += r.usage.completion_tokens or 0
            details = getattr(r.usage, "prompt_tokens_details", None)
            v["gecached"] += getattr(details, "cached_tokens", 0) or 0
        return r.choices[0].message.content or ""

    def _json(self, systeem, invoer, keur):
        """Vraagt JSON tot `keur(data)` geen fouten geeft. Geeft (laatste data of None, fouten) terug."""
        messages = [{"role": "system", "content": systeem}, {"role": "user", "content": invoer}]
        data, fouten = None, ["geen antwoord"]
        for _ in range(POGINGEN):
            try:
                antwoord = normaal(self.vraag_llm(messages))
            except Exception as e:  # een netwerkfout mag het debat niet stoppen
                fouten = [f"LLM-fout: {type(e).__name__}"]
                print(f"LET OP: de Koning kreeg geen antwoord ({type(e).__name__}: {str(e)[-300:]})")
                time.sleep(WACHT)
                continue
            try:
                nieuw = lees_json(antwoord)
                data = nieuw
                fouten = keur(nieuw)
            except (ValueError, TypeError, AttributeError, KeyError) as e:
                fouten = [f"ongeldige vorm: {e}"]
            if not fouten:
                return data, []
            messages += [{"role": "assistant", "content": antwoord},
                         {"role": "user", "content": "Dat klopt nog niet:\n- " + "\n- ".join(fouten)
                          + "\nGeef het volledige, verbeterde JSON-object."}]
        return data, fouten

    def kies(self, beurten, leden):
        rollen = [l.ROL for l in leden]
        gesproken = [b["spreker"] for b in berichten(beurten)]
        uitval = onbeantwoord(beurten)
        bruikbaar = [r for r in rollen if uitval.get(r, 0) < 2] or rollen
        nog_niet = [r for r in bruikbaar if r not in gesproken]
        over = self.budget - len(gesproken)
        te_vroeg = {r for r, eerst in EERST.items() if eerst in bruikbaar and eerst not in gesproken}
        vorige = next((b.get("open_vragen", []) for b in reversed(beurten) if b["soort"] == "routering"), [])

        gesteld = [(b["aan"], b["vraag"]) for b in beurten if b["soort"] == "routering" and b["aan"]]

        def wensen(d):
            """Zachte regels: de Koning krijgt een herkansing, maar ze blokkeren zijn keuze niet."""
            rol, vraag, open_ = d.get("rol"), d.get("vraag") or "", d.get("open_vragen", [])
            fouten = []
            if rol is None:
                if over > 0 and not any("herstel" in v.lower() for _, v in gesteld):
                    fouten.append("je vroeg nog niemand naar veilig herstel; doe dat voor je stopt")
            else:
                if gesproken.count(rol) >= 2 and not is_tekst(d.get("tegenspraak")):
                    fouten.append(f"{rol} sprak al {gesproken.count(rol)} keer; kies een andere Rol, stop, of noem de echte tegenspraak in 'tegenspraak'")
                if any(difflib.SequenceMatcher(None, vraag.lower(), v.lower()).ratio() > 0.75 for r, v in gesteld if r == rol):
                    fouten.append(f"deze vraag lijkt te sterk op een eerdere vraag aan {rol}")
                if len(vraag) > 650 or vraag.count("?") > 3:
                    fouten.append("de vraag is te lang: één hoofdvraag met hooguit één bijvraag, samen hoogstens 80 woorden")
                if len(vorige) - len(open_) > 3:
                    fouten.append(f"open_vragen kromp van {len(vorige)} naar {len(open_)}; hou onbeantwoorde vragen vast")
            if ANDERE_TAAL.search(vraag + " " + d.get("reden", "")):
                fouten.append("schrijf vraag en reden volledig in het Nederlands")
            return fouten

        def eisen(d):
            fouten = []
            if d.get("rol") is None:
                if nog_niet:
                    fouten.append(f"je mag nog niet stoppen, deze Rollen spraken nog niet: {', '.join(nog_niet)}")
            else:
                if d["rol"] not in bruikbaar:
                    fouten.append(f"rol moet null zijn of een van: {', '.join(bruikbaar)}")
                elif over <= len(nog_niet) and d["rol"] not in nog_niet:
                    fouten.append(f"er zijn nog {over} beurten; kies eerst uit: {', '.join(nog_niet)}")
                elif d["rol"] in te_vroeg:
                    fouten.append(f"{EERST[d['rol']]} moet spreken voor {d['rol']}; kies een andere Rol")
                if not is_tekst(d.get("vraag")):
                    fouten.append("vraag ontbreekt")
            if not is_tekst(d.get("reden")):
                fouten.append("reden ontbreekt")
            open_ = d.get("open_vragen", [])
            if not isinstance(open_, list) or not all(isinstance(v, str) for v in open_):
                fouten.append("open_vragen moet een lijst van teksten zijn")
            elif d.get("rol") is not None and not open_:
                fouten.append("open_vragen is leeg terwijl het debat doorgaat: noteer wat nog onzeker is")
            return fouten

        def keur(d):
            return eisen(d) or wensen(d)

        # geschud, zodat de volgorde van de lijst de Koning niet stuurt
        invoer = ("Raadsleden:\n" + "\n".join(f"- {l.ROL} ({l.NAAM}): {l.PERSPECTIEF}" for l in random.sample(leden, len(leden)))
                  + f"\n\nBeurten over: {over} van {self.budget}. Nog niet gesproken: {', '.join(nog_niet) or 'niemand'}."
                  + (f"\nGaf technisch geen antwoord (vraag telt niet als beurt): {', '.join(uitval)}." if uitval else "")
                  + "\nOpen vragen tot nu toe: " + json.dumps(vorige, ensure_ascii=False)
                  + "\n\nDebat:\n\n" + tekst_debat(beurten))
        d, fouten = self._json(KIES, invoer, keur)
        if fouten and d is not None:
            try:
                fouten = eisen(d)  # alleen zachte wensen over: de keuze van de Koning telt
            except (ValueError, TypeError, AttributeError, KeyError):
                pass
        if not fouten:
            vraag = d["vraag"] if d.get("rol") else None
            return d.get("rol"), vraag, d["reden"], d.get("open_vragen", [])
        # Eerlijke terugval: regel in code, en dat staat ook zo in de trace.
        rol = min([r for r in bruikbaar if r not in te_vroeg] or bruikbaar, key=lambda r: (gesproken.count(r), uitval.get(r, 0), rollen.index(r)))
        return (rol, "Welke records in jouw Kennisbron bevestigen of weerleggen wat tot nu toe gezegd is, en wat ontbreekt nog?",
                f"terugval op regel (minst gesproken), want de Koning gaf geen bruikbaar antwoord: {fouten[0]}", vorige)

    def oordeel(self, beurten, run_id):
        """Geeft (verdict, geschillen) terug, geldig volgens schema/ en alleen met ID's uit het debat."""
        ids = bekende_ids(beurten)
        sprekers = {b["spreker"] for b in berichten(beurten)}
        laatste = laatste_dag(beurten)

        def keur(d):
            fouten = []

            def bewijs(waar, x, verplicht=False):
                lijst = x.get("bewijs") or []
                vreemd = [str(i) for i in lijst if i not in ids]
                if vreemd:
                    fouten.append(f"{waar}: geen geldig bewijs: {', '.join(vreemd)}")
                if verplicht and not lijst:
                    fouten.append(f"{waar}: bewijs ontbreekt")

            if not is_tekst(d.get("samenvatting")):
                fouten.append("samenvatting ontbreekt")
            for s in SLOTS:
                slot = d["oorzaak"][s]
                bewijs(s, slot, verplicht=slot.get("bewering") is not None)
            for naam, velden in (("acties", ("actie", "reden")), ("risicos", ("risico", "mitigatie"))):
                if not d.get(naam):
                    fouten.append(f"{naam}: minstens 1 nodig")
                for x in d.get(naam) or []:
                    if not all(is_tekst(x.get(v)) for v in velden):
                        fouten.append(f"{naam}: elk item heeft {' en '.join(velden)} nodig")
                    bewijs(naam, x)
            vt = d.get("vertrouwen") or {}
            if type(vt.get("score")) is not int or not 0 <= vt["score"] <= 100 or not is_tekst(vt.get("reden")):
                fouten.append("vertrouwen: score (geheel getal 0-100) en reden zijn nodig")
            if d.get("voorspelling"):
                bewijs("voorspelling", d["voorspelling"], verplicht=True)
                dag = d["voorspelling"].get("dag")
                if type(dag) is not int or dag < 1:
                    fouten.append("voorspelling: dag moet een geheel getal vanaf 1 zijn")
            for x in d.get("dwaalsporen") or []:
                bewijs("dwaalsporen", x)
                if x.get("van") not in sprekers or not is_tekst(x.get("bewering")) or not is_tekst(x.get("waarom")):
                    fouten.append("dwaalsporen: bewering, waarom en van (een rol die sprak) zijn nodig")
            for g in d.get("geschillen") or []:
                bewijs("geschillen", g, verplicht=g.get("uitkomst") is not None)
                if set(g.get("standpunten") or {}) - sprekers:
                    fouten.append("geschillen: standpunten alleen van Raadsleden die spraken (rolnaam als sleutel)")
            return fouten

        invoer = ("Toegelaten bewijs-ID's: " + (", ".join(sorted(ids)) or "(geen)")
                  + "\n\nDebat:\n\n" + tekst_debat(beurten))

        def volledig(d):
            """Strenger dan keur: vraagt om wat nog leeg is. Leeg mag alleen als het debat er echt niets over zegt."""
            fouten = keur(d)
            if fouten:
                return fouten
            for s in SLOTS:
                if d["oorzaak"][s].get("bewering") is None:
                    fouten.append(f"{s} is leeg: vul het in met wat het debat steunt en een lage zekerheid; alleen null als het debat er niets over zegt")
                elif GEEN_BESLUIT.search(d["oorzaak"][s]["bewering"]):
                    fouten.append(f"{s}: de bewering is geen besluit (\"{GEEN_BESLUIT.search(d['oorzaak'][s]['bewering']).group(0)}\"); geef de best passende verklaring en zet de twijfel in zekerheid")
                elif s == "mechanisme" and len({i[:3] for i in d["oorzaak"][s]["bewijs"]}) < 2:
                    fouten.append("mechanisme: steun de keten op records uit minstens twee bronnen, met de regels die de symptomen verklaren")
                elif not isinstance(d["oorzaak"][s].get("zekerheid"), (int, float)):
                    fouten.append(f"{s}: zekerheid ontbreekt")
            for naam, tekst in (("verloop", d["oorzaak"]["verloop"].get("bewering")), ("samenvatting", d["samenvatting"])):
                if tijdsfout(tekst):
                    fouten.append(f"{naam}: niet in de volgorde van de tijd ({tijdsfout(tekst)})")
            if d.get("voorspelling") and d["voorspelling"]["dag"] < laatste:
                fouten.append(f"voorspelling: dag {d['voorspelling']['dag']} ligt vóór dag {laatste}, waarover het debat al spreekt; reken verder vanaf het einde van het ritme")
            if not d.get("voorspelling"):
                fouten.append("voorspelling ontbreekt: leid ze af uit het patroon in het debat, of null als er geen patroon is")
            return fouten

        def vraag(tekst):
            d, fouten = self._json(OORDEEL, tekst, volledig)
            if d is not None and fouten:
                try:
                    fouten = keur(d)  # eerlijk onbekend mag, ongeldig niet
                except (ValueError, TypeError, AttributeError, KeyError) as e:
                    fouten = [f"ongeldige vorm: {e}"]
            return d, fouten

        d, fouten = vraag(invoer)
        if d is None or fouten:  # tweede kans met het andere model, zodat het Verdict niet leeg blijft
            print("LET OP: geen geldig Verdict, nieuwe poging met het andere model.")
            vorig_model = self.model
            self.model = "MODEL_KLEIN" if vorig_model == "MODEL_GROOT" else "MODEL_GROOT"
            d2, f2 = vraag(invoer)
            self.model = vorig_model
            if d2 is not None and (d is None or not f2):
                d, fouten = d2, f2
        if d is not None and not fouten:  # tweede blik: de Koning toetst zijn eigen Verdict aan het debat
            beter, f2 = vraag(invoer + "\n\n" + CONTROLE.replace("{verdict}", json.dumps(d, ensure_ascii=False, indent=1)))
            if beter is not None and not f2 and not verzwakt(d, beter, laatste):
                d = beter
                c = d.get("correcties")
                self.correcties = [x for x in c if is_tekst(x)] if isinstance(c, list) else []
            else:
                print("LET OP: de controleronde gaf geen bruikbaar Verdict; het eerste Verdict blijft staan.")
        if fouten:
            print("LET OP: het Verdict bevatte nog fouten en is opgeschoond: " + "; ".join(fouten))
        return schoon(d or {}, run_id, ids, sprekers)


def verzwakt(oud, nieuw, laatste=0):
    """Maakte de controle het Verdict zwakker (minder bewijs, zekerheid of onderdelen)? Dan blijft het oude staan."""
    for s in SLOTS:
        o, n = oud["oorzaak"][s], nieuw["oorzaak"][s]
        if o.get("bewering") and not n.get("bewering"):
            return True
        if not o.get("bewering"):
            continue
        if not GEEN_BESLUIT.search(o["bewering"]) and GEEN_BESLUIT.search(n["bewering"]):
            return True
        if len(n.get("bewijs") or []) < 0.8 * len(o.get("bewijs") or []):
            return True
        zo, zn = o.get("zekerheid"), n.get("zekerheid")
        if isinstance(zo, (int, float)) and isinstance(zn, (int, float)) and zo - zn > 0.101:
            return True
    if any(len(nieuw.get(x) or []) < len(oud.get(x) or []) for x in ("acties", "dwaalsporen", "keten")):
        return True
    vo, vn = oud.get("voorspelling") or {}, nieuw.get("voorspelling") or {}
    if vo and not vn:
        return True
    # een voorspelling die al klopte met de tijd, verandert niet meer
    return bool(vo) and vo.get("dag") != vn.get("dag") and vo.get("dag", 0) >= laatste


def laatste_dag(beurten):
    """De laatste dag die een Raadslid in het debat noemde."""
    dagen = [0]
    for b in berichten(beurten):
        for m in re.finditer(r"\bdag(?:en)?\s+(\d{1,3}(?:(?:\s*,\s*|\s+en\s+|\s+t/m\s+|\s+tot en met\s+)\d{1,3})*)", b["tekst"], re.I):
            dagen += [int(x) for x in re.findall(r"\d+", m.group(1))]
    return max(dagen)


def tijdsfout(tekst):
    """Zoekt in één zin tijden van dezelfde dag die achteruit lopen. Geeft een uitleg of None."""
    for zin in re.split(r"(?<=[.!?;])\s+", tekst or ""):
        dag, vorige = None, None
        for m in re.finditer(r"\bdag\s+(\d+)|\b(\d{1,2}):(\d{2})\b", zin, re.I):
            if m.group(1):
                if m.group(1) != dag:
                    dag, vorige = m.group(1), None
                continue
            nu = int(m.group(2)) * 60 + int(m.group(3))
            if vorige is not None and nu < vorige[0]:
                return f"{vorige[1]} staat vóór {m.group(0)}"
            vorige = (nu, m.group(0))
    return None


def schoon(d, run_id, ids, sprekers):
    """Laat alleen over wat geldig is; wat niet bewezen is, wordt eerlijk null."""
    def obj(x):
        return x if isinstance(x, dict) else {}

    def lijst(x):
        return [i for i in x if isinstance(i, dict)] if isinstance(x, list) else []

    def bewijs(x):
        b = x.get("bewijs")
        return list(dict.fromkeys(i for i in b if isinstance(i, str) and i in ids)) if isinstance(b, list) else []

    oorzaak = {}
    for s in SLOTS:
        bron = obj(obj(d.get("oorzaak")).get(s))
        b, z = bewijs(bron), bron.get("zekerheid")
        ok = is_tekst(bron.get("bewering")) and b
        oorzaak[s] = {"bewering": bron["bewering"] if ok else None, "bewijs": b if ok else []}
        if ok and isinstance(z, (int, float)) and not isinstance(z, bool):
            oorzaak[s]["zekerheid"] = max(0, min(1, z))
    acties = [{"actie": a["actie"], "reden": a["reden"], **({"bewijs": bewijs(a)} if bewijs(a) else {})}
              for a in lijst(d.get("acties")) if is_tekst(a.get("actie")) and is_tekst(a.get("reden"))]
    risicos = [{"risico": r["risico"], "mitigatie": r["mitigatie"]}
               for r in lijst(d.get("risicos")) if is_tekst(r.get("risico")) and is_tekst(r.get("mitigatie"))]
    # Nooit TODO: zonder bruikbaar antwoord van de Koning staat er eerlijk dat het niet gelukt is.
    verdict = {
        "run_id": run_id,
        "samenvatting": d["samenvatting"] if is_tekst(d.get("samenvatting"))
        else "De Council kwam niet tot een onderbouwd Verdict: de Koning gaf geen geldig antwoord.",
        "oorzaak": oorzaak,
        "acties": acties or [{"actie": "Het debat opnieuw voeren en geen onomkeerbare ingreep doen tot er een onderbouwde oorzaak is.",
                              "reden": "Deze Council kwam niet tot een herstelplan."}],
        "risicos": risicos or [{"risico": "Ingrijpen zonder vastgestelde oorzaak kan de toestand verergeren.",
                                "mitigatie": "Eerst de oorzaak met bewijs vaststellen."}],
    }
    if not (is_tekst(d.get("samenvatting")) and acties and risicos):
        verdict["onvolledig"] = True  # extra veld: validate waarschuwt hierop
    keten = [{"stap": k["stap"], "regel": k["regel"] if is_tekst(k.get("regel")) else "", "bewijs": bewijs(k)}
             for k in lijst(d.get("keten")) if is_tekst(k.get("stap")) and bewijs(k)]
    if keten:
        verdict["keten"] = keten  # extra veld: van ingreep naar symptoom, per stap met bewijs
    dwaal = [{"bewering": x["bewering"], "van": x["van"], "waarom": x["waarom"], "bewijs": bewijs(x)}
             for x in lijst(d.get("dwaalsporen")) if x.get("van") in sprekers and is_tekst(x.get("bewering")) and is_tekst(x.get("waarom"))]
    if dwaal:
        verdict["dwaalsporen"] = dwaal  # extra veld: wat de Council onderzocht en verwierp
    vt = obj(d.get("vertrouwen"))
    if type(vt.get("score")) is int and 0 <= vt["score"] <= 100 and is_tekst(vt.get("reden")):
        verdict["vertrouwen"] = {"score": vt["score"], "reden": vt["reden"]}  # extra veld: hoe zeker de Koning is
    v = obj(d.get("voorspelling"))
    if type(v.get("dag")) is int and v["dag"] >= 1 and is_tekst(v.get("bewering")) and bewijs(v):
        verdict["voorspelling"] = {"dag": v["dag"], "bewering": v["bewering"], "bewijs": bewijs(v)}

    geschillen = []
    for g in lijst(d.get("geschillen")):
        standpunten = {r: s for r, s in obj(g.get("standpunten")).items() if r in sprekers and is_tekst(s)}
        if is_tekst(g.get("onderwerp")) and standpunten:
            b = bewijs(g)
            geschillen.append({"onderwerp": g["onderwerp"], "standpunten": standpunten,
                               "uitkomst": g["uitkomst"] if is_tekst(g.get("uitkomst")) and b else None, "bewijs": b})
    return verdict, geschillen


def toespraak(verdict):
    """Het slotbericht van de Koning in de trace: het Verdict in gewone tekst."""
    regels = [verdict["samenvatting"]]
    for s in SLOTS:
        x = verdict["oorzaak"][s]
        regels.append(f"**{s.capitalize()}**: {x['bewering'] or 'onbekend'} {', '.join(x['bewijs'])}".strip())
    if verdict.get("vertrouwen"):
        regels.append(f"**Vertrouwen**: {verdict['vertrouwen']['score']}%. {verdict['vertrouwen']['reden']}")
    return "\n\n".join(regels)
