"""Council of Achrona: een mini-Council met Raadsleden en een Koning.

Start met `uv run council`. Schrijft verdict.json en trace.json in de root van de repo.
De Koning leidt het debat in `kies_volgende()`: hij beslist wie spreekt, met welke vraag en waarom.
"""
import asyncio
import json
import os
import re
import sys
import uuid
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path

from agent_framework import Agent, MCPStreamableHTTPTool
from agent_framework.openai import OpenAIChatCompletionClient
from dotenv import load_dotenv
from openai import AsyncOpenAI

import raadsleden
from koning import Koning, normaal, onbeantwoord, toespraak, vind_ids
from scores import geef_scores

ROOT = Path(__file__).parent
KENNISBRONNEN = ROOT / "kennisbronnen"
ROLLEN = ("archmage", "engineer", "guardian_keeper", "historian", "oracle", "beast_master")
POGINGEN = 5  # per Raadslid-beurt
MAX_UITVAL = 3  # zoveel mislukte beurten, dan stopt het debat en volgt toch een Verdict
# Zwakke modellen geven soms hun (Engelse) gedachten in plaats van een antwoord.
GEDACHTEN = re.compile(r"\b(we need to|we have to|let's|let me|i need to|i should|the king's question|the user)\b", re.I)
WACHT = 40  # seconden na een fout: gratis providers hebben vaak een limiet per minuut
BEURTENBUDGET = 10  # spelregel: maximaal 10 Raadslid-beurten (berichten van Raadsleden) per run


def kies_volgende(beurten, leden, koning):
    """De Koning leidt het debat: een LLM leest het debat en beslist wie spreekt (zie koning.py).

    Geeft (rol, vraag, reden, open_vragen) terug; rol None = de Koning stopt het debat.
    """
    return koning.kies(beurten, leden)


def controleer(leden):
    """Duidelijke melding in plaats van een traceback bij de meest voorkomende fouten."""
    mist = [v for v in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "MODEL_KLEIN", "MODEL_GROOT") if not os.getenv(v)]
    if mist:
        sys.exit(f"In .env ontbreekt: {', '.join(mist)}. Kopieer .env.example naar .env en vul het in, of zie 'Vooraf installeren' in de README.")
    for lid in leden:
        if lid.ROL not in ROLLEN:
            sys.exit(f"raadsleden/: ROL = {lid.ROL!r} is ongeldig. Gebruik de mapnaam van de Rol: {', '.join(ROLLEN)}.")
    dubbel = {l.ROL for l in leden if [x.ROL for x in leden].count(l.ROL) > 1}
    if dubbel:
        sys.exit(f"Eén Raadslid per Rol: {', '.join(dubbel)} komt meer dan eens voor in raadsleden/.")


def client(model_env):
    key = os.environ["OPENAI_API_KEY"]
    return OpenAIChatCompletionClient(
        model=os.environ[model_env],
        async_client=AsyncOpenAI(
            base_url=os.environ["OPENAI_BASE_URL"],
            api_key=key,
            max_retries=8,  # bij een rate limit (429) wacht de client en probeert opnieuw
        ),
    )


def kennisbron_bestanden(rol):
    map_ = KENNISBRONNEN / rol
    return sorted(p for p in map_.glob("*") if p.suffix in (".md", ".csv")) if map_.is_dir() else []


def instructies(lid, bestanden, mcp):
    tekst = (
        f"Je bent de {lid.NAAM}, een Raadslid in de Council of Achrona. {lid.PERSPECTIEF}\n"
        "Je debatteert met de andere Raadsleden over de oorzaak van de Desync. "
        "Baseer je alleen op je eigen Kennisbron en noem bij elke bewering de record-ID's precies zoals ze in je Kennisbron staan. Verzin nooit een ID. "
        "Zeg het eerlijk als je iets niet weet.\n"
        "Beantwoord de vraag van de Koning eerst, in hoogstens 200 woorden. Herhaal niet wat al gezegd is. "
        "Noem exacte dagen, getallen en namen uit je records. Scheid wat je records tonen van wat je vermoedt. "
        "Wat een ander zegt is geen bewijs: spreek tegen als je eigen records iets anders tonen, ook als het je Belang niet uitkomt. "
        "Noem ook de regels, normen, voorwaarden of procedures uit je bron die bij de vraag horen. "
        "Is een record jouw eigen logboek, brief of notitie, zeg dan eerlijk in de ik-vorm wat je zelf deed. "
        "Je ziet de bron van anderen niet en dat is normaal: meld dat niet. Verwijs hoogstens in één zin naar een Raadslid dat verder kan helpen. "
        f"Er zijn alleen deze Raadsleden: {', '.join(l.NAAM for l in raadsleden.alle())}."
    )
    if mcp:
        tekst += ("\nLees elke beurt eerst je volledige Kennisbron met lees_kennisbron, want je onthoudt niets van vorige beurten. "
                  "Geef daarna alleen je afgewerkte antwoord in het Nederlands, geen plannen of gedachten.")
    elif bestanden:
        tekst += "\n\nJe Kennisbron:\n\n" + "\n\n".join(f"## {p.name}\n\n{p.read_text(encoding='utf-8')}" for p in bestanden)
    else:
        tekst += "\nJe hebt nog geen Kennisbron: zeg dat je niets kan bewijzen."
    return tekst


def gelezen_ids(response, rol):
    """Record-ID's van de eigen Rol in de toolresultaten: alleen om genoemde ID's te controleren, niet voor de Koning."""
    ids = set()
    for msg in response.messages:
        for c in msg.contents:
            if c.type == "function_result":
                ids |= vind_ids(str(c.result or ""), rol)
                for item in c.items or []:
                    ids |= vind_ids(str(getattr(item, "text", "") or ""), rol)
    return ids


def tel(verbruik, model, usage):
    if not usage:
        return
    v = verbruik.setdefault(model, {"beurten": 0, "invoer": 0, "gecached": 0, "uitvoer": 0})
    v["beurten"] += 1  # één beurt = één agent-run (met MCP soms meerdere model-requests)
    v["invoer"] += usage.get("input_token_count") or 0
    v["gecached"] += usage.get("cache_read_input_token_count") or 0
    v["uitvoer"] += usage.get("output_token_count") or 0


def toolcalls(response):
    for msg in response.messages:
        for c in msg.contents:
            if c.type == "function_call":
                args = c.arguments
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args else {}
                    except ValueError:
                        args = {"ruw": args}  # ongeldige JSON van het model: toch in de trace
                yield c.name, dict(args) if isinstance(args, dict) else {"ruw": str(args)} if args else {}


async def run():
    load_dotenv(ROOT / ".env")
    opties = {"reasoning_effort": os.environ["REASONING"]} if os.getenv("REASONING") else None  # minder denkwerk = minder tokens
    run_id = f"{datetime.now().astimezone().isoformat(timespec='seconds')}_{uuid.uuid4().hex[:8]}"
    leden = raadsleden.alle()
    controleer(leden)
    beurten, verbruik = [], {}
    trace = {"run_id": run_id, "status": "bezig", "raadsleden": [l.ROL for l in leden], "beurten": beurten, "geschillen": [], "verbruik": verbruik}

    def bewaar(naam, data):
        (ROOT / naam).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def beurt(ronde, spreker, soort, **velden):
        beurten.append({"nr": len(beurten) + 1, "ronde": ronde, "spreker": spreker, "soort": soort, **velden})
        bewaar("trace.json", trace)  # na elke beurt: de viewer (`uv run viewer`) toont het debat live

    klein = client("MODEL_KLEIN")
    koning = Koning(BEURTENBUDGET)
    verbruik["groot"] = koning.verbruik
    gezien = {lid.ROL: set() for lid in leden}  # ID's die elk Raadslid echt in zijn Kennisbron zag

    ronde, open_vragen = 1, []
    try:
        async with AsyncExitStack() as stack:
            agents = []
            for lid in leden:
                mcp_url = os.getenv(f"MCP_{lid.ROL.upper()}", "").strip()
                bestanden = [] if mcp_url else kennisbron_bestanden(lid.ROL)
                tools = []
                if mcp_url:
                    try:
                        tools.append(await stack.enter_async_context(MCPStreamableHTTPTool(name=f"kennisbron_{lid.ROL}", url=mcp_url)))
                    except Exception as e:
                        sys.exit(f"MCP voor {lid.NAAM} ({mcp_url}) is onbereikbaar: {e}\n"
                                 f"Is de server al open (vanaf 10:00)? Klopt het pad /{lid.ROL}/mcp? "
                                 f"Of laat MCP_{lid.ROL.upper()} leeg om de bestanden uit kennisbronnen/{lid.ROL}/ te gebruiken.")
                elif not bestanden:
                    print(f"LET OP: {lid.NAAM} heeft geen Kennisbron (MCP_{lid.ROL.upper()} is leeg en kennisbronnen/{lid.ROL}/ ook).")
                for p in bestanden:  # fallback zonder MCP: welke bestanden gingen in de prompt
                    gezien[lid.ROL] |= vind_ids(p.read_text(encoding="utf-8"), lid.ROL)
                    beurt(0, lid.ROL, "bestand", bestand=f"{lid.ROL}/{p.name}")
                agents.append((lid, mcp_url, Agent(klein, instructies(lid, bestanden, mcp_url), name=lid.ROL, tools=tools)))

            (ROOT / "verdict.json").unlink(missing_ok=True)  # setup gelukt: geen Verdict van een vorige run laten staan
            bewaar("trace.json", trace)
            per_rol = {lid.ROL: (lid, mcp_url, agent) for lid, mcp_url, agent in agents}
            debat = []
            while True:
                gesproken = sum(1 for b in beurten if b["soort"] == "bericht" and b["spreker"] in per_rol)
                if gesproken >= BEURTENBUDGET:
                    rol, vraag, reden = None, None, f"Beurtenbudget op ({BEURTENBUDGET} Raadslid-beurten)"  # open_vragen blijft staan
                elif sum(onbeantwoord(beurten).values()) >= MAX_UITVAL:
                    rol, vraag, reden = None, None, f"technische uitval: {MAX_UITVAL} keer gaf een Raadslid geen antwoord"
                else:
                    trace["aan_het_woord"] = "koning"
                    bewaar("trace.json", trace)
                    # in een thread: de Koning wacht op de LLM zonder de MCP-verbindingen te blokkeren
                    rol, vraag, reden, open_vragen = await asyncio.to_thread(kies_volgende, beurten, leden, koning)
                if rol is not None and rol not in per_rol:
                    sys.exit(f"kies_volgende() koos {rol!r}, maar die Rol zit niet in raadsleden/.")
                if rol is not None:
                    ronde = gesproken // len(leden) + 1
                # Elke keuze van de Koning staat in de trace: aan wie, welke vraag en waarom.
                beurt(ronde, "koning", "routering", aan=rol, vraag=vraag, reden=reden, open_vragen=open_vragen)
                print(f"\n[Koning] {'stopt' if rol is None else '→ ' + per_rol[rol][0].NAAM}: {reden}")
                if rol is None:
                    break
                lid, mcp_url, agent = per_rol[rol]
                # Wie ziet wat? Hier ziet elk Raadslid het hele debat (broadcast). Ook dat is een keuze.
                prompt = "Debat tot nu toe:\n\n" + ("\n\n".join(debat) or "(nog niets)") + f"\n\nVraag aan jou: {vraag}"
                kort = "Laatste bijdragen uit het debat:\n\n" + ("\n\n".join(debat[-2:]) or "(nog niets)") + f"\n\nVraag aan jou: {vraag}"
                trace["aan_het_woord"] = rol
                bewaar("trace.json", trace)
                controle, tekst = {}, ""
                for poging in range(1, POGINGEN + 1):  # herkansing bij een fout of een antwoord zonder bron of bewijs
                    try:
                        # na twee mislukte pogingen: een korter debat meesturen, want kleine modellen hebben een limiet
                        r = await agent.run(prompt if poging < 3 else kort, options=opties)
                    except Exception as e:
                        trace.setdefault("mislukte_pogingen", []).append({"rol": rol, "na_beurt": len(beurten), "poging": poging, "fout": type(e).__name__})
                        print(f"LET OP: {lid.NAAM}, poging {poging}: {type(e).__name__}: {str(e)[-300:]}")
                        if poging < POGINGEN:
                            await asyncio.sleep(WACHT)
                        continue
                    tel(verbruik, "klein", r.usage_details)
                    for tool, args in toolcalls(r):
                        beurt(ronde, rol, "toolcall", tool=tool, argumenten=args, pad=mcp_url)
                    gezien[rol] |= gelezen_ids(r, rol)
                    if not r.text.strip() or GEDACHTEN.search(r.text):
                        trace.setdefault("mislukte_pogingen", []).append({"rol": rol, "na_beurt": len(beurten), "poging": poging, "fout": "geen afgewerkt antwoord"})
                        continue  # leeg of geen afgewerkt antwoord: telt niet
                    tekst = normaal(r.text)  # gewone koppeltekens in ID's
                    if vind_ids(tekst, rol) & gezien[rol] or poging >= 2:  # zonder bewijs: één herkansing
                        break
                if poging > 1:
                    controle["pogingen"] = poging  # eerlijk zichtbaar in de trace
                if not tekst.strip():
                    # geen bericht in de trace; de Koning ziet dat zijn vraag onbeantwoord bleef en gaat verder
                    trace.setdefault("uitval", []).append({"rol": rol, "na_beurt": len(beurten), "pogingen": poging})
                    bewaar("trace.json", trace)
                    print(f"LET OP: {lid.NAAM} gaf na {poging} pogingen geen bruikbaar antwoord.")
                    continue
                if niet_gezien := sorted(vind_ids(tekst, rol) - gezien[rol]):
                    controle["ids_niet_in_bron"] = niet_gezien
                if not gezien[rol]:
                    controle["zonder_bron"] = True
                beurt(ronde, rol, "bericht", tekst=tekst, **controle)
                if controle:
                    print(f"LET OP: {lid.NAAM}: {controle}")
                debat.append(f"{lid.NAAM}: {tekst}")
                print(f"\n[ronde {ronde}] {lid.NAAM}:\n{tekst}")
    except Exception as e:
        if not any(b["soort"] == "bericht" for b in beurten):
            raise  # nog niets gezegd: er is niets om een Verdict op te bouwen
        print(f"LET OP: het debat stopte door een technische fout ({type(e).__name__}: {str(e)[-300:]}). De Koning oordeelt met wat er is.")
        if not (beurten[-1]["soort"] == "routering" and beurten[-1]["aan"] is None):
            beurt(ronde, "koning", "routering", aan=None, vraag=None, open_vragen=open_vragen,
                  reden=f"het debat stopte door een technische fout ({type(e).__name__})")

    # De Koning heeft geen Rol en geen Kennisbron: hij kent alleen wat de Raadsleden inbrachten.
    trace["aan_het_woord"] = "koning"
    bewaar("trace.json", trace)
    verdict, trace["geschillen"] = await asyncio.to_thread(koning.oordeel, beurten, run_id)
    trace["correcties_koning"] = koning.correcties
    tekst = toespraak(verdict)
    beurt(ronde + 1, "koning", "bericht", tekst=tekst)
    print(f"\n[Koning]:\n{tekst}")

    trace["verdict_klaar"] = True
    bewaar("verdict.json", verdict)  # eerst het Verdict: een fout bij de scores mag het niet kosten
    trace["aan_het_woord"] = None
    bewaar("trace.json", trace)
    print("\nDe Raadsleden en de Koning geven elkaar scores...")
    klein_scores = Koning(BEURTENBUDGET, model="MODEL_KLEIN")
    try:
        trace["scores"] = await asyncio.to_thread(geef_scores, beurten, leden, koning, klein_scores, verdict)
        verbruik["scores"] = klein_scores.verbruik
        for rol, s in sorted(trace["scores"]["gemiddelden"].items(), key=lambda x: -x[1]["score"]):
            print(f"  {rol}: {s['score']}/10 ({s['aantal']} stemmen)")
    except Exception as e:
        print(f"LET OP: geen scores ({type(e).__name__}: {e})")
    trace["status"] = "klaar"
    bewaar("trace.json", trace)
    kopie = ROOT / "runs" / run_id.replace(":", "")  # een volgende run overschrijft de root: hou elke run bij
    kopie.mkdir(parents=True, exist_ok=True)
    for naam in ("trace.json", "verdict.json"):
        (kopie / naam).write_bytes((ROOT / naam).read_bytes())

    print(f"\nKlaar: verdict.json en trace.json geschreven (run_id {run_id}).")
    for model, v in verbruik.items():
        print(f"Verbruik {model}: {v['beurten']} beurten, {v['invoer']} tokens in ({v['gecached']} gecached), {v['uitvoer']} tokens uit")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        asyncio.run(run())
    except (Exception, KeyboardInterrupt) as e:
        # laat geen trace achter die eeuwig "bezig" lijkt
        pad = ROOT / "trace.json"
        trace = json.loads(pad.read_text(encoding="utf-8")) if pad.exists() else {}
        if trace.get("status") == "bezig":
            trace.update(status="afgebroken", aan_het_woord=None, fout=type(e).__name__)
            pad.write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")
            print("\nDe run is afgebroken: deze trace is onvolledig en niet om in te leveren. Een vorige goede run staat in runs/.")
        raise


if __name__ == "__main__":
    main()
