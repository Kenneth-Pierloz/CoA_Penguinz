"""De Koning als agent: kiest wie spreekt en stelt het Verdict op.

De Koning heeft geen tools en geen Kennisbron. Hij ziet alleen het debat (`beurten` uit de trace).
Gewone, niet-async code: council.py roept dit op in een aparte thread.
"""
import json
import os
import re
import time

from openai import OpenAI

PREFIX = {"ARC": "archmage", "ENG": "engineer", "GUA": "guardian_keeper",
          "HIS": "historian", "ORA": "oracle", "BEA": "beast_master"}
TIJDEN = ("verleden", "heden", "toekomst")
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


def tijd(lid):
    t = getattr(lid, "TIJD", "heden")
    return t if t in TIJDEN else "heden"


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
- Laat een bewering toetsen door de Rol wiens bron ze kan bevestigen of weerleggen. Samenvallen in de tijd is nog geen oorzaak.
- Verwijst iemand naar een andere Rol, vraag het aan die Rol.
- Spreken Raadsleden elkaar tegen, leg het conflict scherp voor aan wie het kan beslechten.
- Zoek eerst het deel van de oorzaak dat het minst bewezen is. Vraag ook naar wat veilig herstel vraagt en wat kan mislopen.
- Laat niemand herhalen. Stel één scherpe, concrete vraag die met records te beantwoorden is.
- Vraag niet vaag om "je inbreng". Wie iets zegt dat tegen zijn eigen Belang ingaat, is extra geloofwaardig; wie het goed uitkomt, vraagt om toetsing.
- Voorstel, geen plicht: laat je leiden door de tijd. Begin bij wie naar het verleden kijkt, ga dan naar het heden, en eindig bij de toekomst. Wijk gerust af als een tegenspraak, een verwijzing of een te toetsen bewering dringender is, en zeg in de reden dan waarom.
- Werk open_vragen bij: schrap alleen wat met bewijs beantwoord is, voeg nieuwe toe. Zolang ingreep, mechanisme, verloop, herstel of voorspelling onzeker is, hoort daar een open vraag bij.
- Stop (rol null) zodra elk deel met bewijs uit meer dan één bron onderbouwd is en belangrijke tegenspraken beslecht zijn, liefst vóór het budget op is. Noem in de reden waarom je genoeg weet en wat open blijft.

De reden is concreet: verwijs naar wie wat zei. Antwoord in het Nederlands met alleen dit JSON-object:
{"rol": "<rol>" of null, "vraag": "<vraag>" of null, "reden": "<1-3 zinnen>", "open_vragen": ["..."]}"""

OORDEEL = """Je bent de Koning van de Council of Achrona. Het debat is klaar. Stel het Council Verdict op, uitsluitend uit het debat. Je hebt zelf geen Kennisbron.

Regels:
- Weeg bewijs, tel geen stemmen. Een spoor dat in het debat weerlegd is, hoort niet in de oorzaak.
- Gebruik als bewijs alleen ID's uit de gegeven lijst, en alleen als dat record de bewering echt steunt. Neem per deel het sterkste bewijs, bij voorkeur uit meerdere bronnen.
- Wees precies in de oorzaak: wie, wat, welke dag; de hele keten van ingreep naar symptomen; het ritme of patroon in de tijd met dagen en getallen.
- Weet je een deel niet, dan bewering null en bewijs []. Gok niet.
- acties staan in uitvoeringsvolgorde en houden rekening met afhankelijkheden uit het debat.
- risicos gaan over wat bij herstel of bij niets doen kan mislopen, elk met een mitigatie.
- voorspelling: dag (geheel getal) waarop de Desync opnieuw toeslaat als niemand ingrijpt, met de berekening. Geen basis in het debat? Dan null.
- zekerheid is 0 tot 1 en eerlijk.
- geschillen: echte tegenspraken tussen Raadsleden in dit debat, met ieders standpunt. uitkomst null en bewijs [] als het niet beslecht is. Geen tegenspraak? Dan [].

Antwoord in het Nederlands met alleen dit JSON-object:
{"samenvatting": "...",
 "oorzaak": {"ingreep": {"bewering": "...", "bewijs": ["ID"], "zekerheid": 0.0}, "mechanisme": {...}, "verloop": {...}},
 "acties": [{"actie": "...", "reden": "...", "bewijs": ["ID"]}],
 "risicos": [{"risico": "...", "mitigatie": "..."}],
 "voorspelling": {"dag": 1, "bewering": "...", "bewijs": ["ID"]} of null,
 "geschillen": [{"onderwerp": "...", "standpunten": {"<rol>": "..."}, "uitkomst": "..." of null, "bewijs": ["ID"]}]}"""


class Koning:
    def __init__(self, budget, vraag_llm=None):
        self.budget = budget
        self.verbruik = {"beurten": 0, "invoer": 0, "gecached": 0, "uitvoer": 0}
        self.vraag_llm = vraag_llm or self._llm  # vervangbaar, om zonder API te testen
        self._client = None

    def _llm(self, messages):
        if self._client is None:
            self._client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["OPENAI_API_KEY"],
                                  max_retries=8, timeout=180)
        extra = {"reasoning_effort": os.environ["REASONING"]} if os.getenv("REASONING") else {}
        r = self._client.chat.completions.create(model=os.environ["MODEL_GROOT"], messages=messages, **extra)
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
        nog_niet = [r for r in rollen if r not in gesproken]
        over = self.budget - len(gesproken)
        vorige = next((b.get("open_vragen", []) for b in reversed(beurten) if b["soort"] == "routering"), [])

        def keur(d):
            fouten = []
            if d.get("rol") is None:
                if nog_niet:
                    fouten.append(f"je mag nog niet stoppen, deze Rollen spraken nog niet: {', '.join(nog_niet)}")
            else:
                if d["rol"] not in rollen:
                    fouten.append(f"rol moet null zijn of een van: {', '.join(rollen)}")
                elif over <= len(nog_niet) and d["rol"] not in nog_niet:
                    fouten.append(f"er zijn nog {over} beurten; kies eerst uit: {', '.join(nog_niet)}")
                if not is_tekst(d.get("vraag")):
                    fouten.append("vraag ontbreekt")
            if not is_tekst(d.get("reden")):
                fouten.append("reden ontbreekt")
            open_ = d.get("open_vragen", [])
            if not isinstance(open_, list) or not all(isinstance(v, str) for v in open_):
                fouten.append("open_vragen moet een lijst van teksten zijn")
            return fouten

        invoer = ("Raadsleden:\n" + "\n".join(f"- {l.ROL} ({l.NAAM}, kijkt naar het {tijd(l)}): {l.PERSPECTIEF}".replace("het toekomst", "de toekomst")
                                              for l in sorted(leden, key=lambda l: TIJDEN.index(tijd(l))))
                  + f"\n\nBeurten over: {over} van {self.budget}. Nog niet gesproken: {', '.join(nog_niet) or 'niemand'}."
                  + "\nOpen vragen tot nu toe: " + json.dumps(vorige, ensure_ascii=False)
                  + "\n\nDebat:\n\n" + tekst_debat(beurten))
        d, fouten = self._json(KIES, invoer, keur)
        if not fouten:
            vraag = d["vraag"] if d.get("rol") else None
            return d.get("rol"), vraag, d["reden"], d.get("open_vragen", [])
        # Eerlijke terugval: regel in code, en dat staat ook zo in de trace.
        rol = min(rollen, key=lambda r: (gesproken.count(r), rollen.index(r)))
        return (rol, "Welke records in jouw Kennisbron bevestigen of weerleggen wat tot nu toe gezegd is, en wat ontbreekt nog?",
                f"terugval op regel (minst gesproken), want de Koning gaf geen bruikbaar antwoord: {fouten[0]}", vorige)

    def oordeel(self, beurten, run_id):
        """Geeft (verdict, geschillen) terug, geldig volgens schema/ en alleen met ID's uit het debat."""
        ids = bekende_ids(beurten)
        sprekers = {b["spreker"] for b in berichten(beurten)}

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
            if d.get("voorspelling"):
                bewijs("voorspelling", d["voorspelling"], verplicht=True)
                dag = d["voorspelling"].get("dag")
                if type(dag) is not int or dag < 1:
                    fouten.append("voorspelling: dag moet een geheel getal vanaf 1 zijn")
            for g in d.get("geschillen") or []:
                bewijs("geschillen", g, verplicht=g.get("uitkomst") is not None)
                if set(g.get("standpunten") or {}) - sprekers:
                    fouten.append("geschillen: standpunten alleen van Raadsleden die spraken (rolnaam als sleutel)")
            return fouten

        d, fouten = self._json(OORDEEL, "Toegelaten bewijs-ID's: " + (", ".join(sorted(ids)) or "(geen)")
                               + "\n\nDebat:\n\n" + tekst_debat(beurten), keur)
        if fouten:
            print("LET OP: het Verdict bevatte nog fouten en is opgeschoond: " + "; ".join(fouten))
        return schoon(d or {}, run_id, ids, sprekers)


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
    verdict = {
        "run_id": run_id,
        "samenvatting": d["samenvatting"] if is_tekst(d.get("samenvatting")) else "TODO",  # validate waarschuwt hierop
        "oorzaak": oorzaak,
        "acties": acties or [{"actie": "TODO", "reden": "TODO"}],
        "risicos": risicos or [{"risico": "TODO", "mitigatie": "TODO"}],
    }
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
    return "\n\n".join(regels)
