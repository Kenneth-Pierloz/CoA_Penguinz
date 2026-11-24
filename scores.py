"""Feedbackscores: de Raadsleden en de Koning beoordelen elkaars bijdragen, na het debat en het Verdict.

Iedereen ziet alleen het debat (geen tools, geen Kennisbron) en niemand scoort zichzelf. De scores veranderen
het Verdict niet en tellen niet als Raadslid-beurt. Ze staan in trace.json onder `scores`.
"""
from concurrent.futures import ThreadPoolExecutor

from koning import berichten, is_tekst, tekst_debat

BEOORDEEL = """Je bent {wie} in de Council of Achrona. Het debat is voorbij. Geef elk van deze Raadsleden een score voor hun bijdrage: {wie_te_scoren}.

Let op vier dingen, die even zwaar wegen:
- bewijs: noemde dit Raadslid record-ID's die de beweringen steunen, met dagen en getallen?
- antwoord: ging het in op de vraag van de Koning en bracht het de zaak vooruit?
- eerlijkheid: scheidde het feit van vermoeden en gaf het fouten toe, ook tegen het eigen Belang in?
- samenwerking: toetste of weerlegde het beweringen van anderen, zonder te herhalen?

Score: een geheel getal van 1 (zwak) tot 10 (uitstekend). 5 is gemiddeld; gebruik de hele schaal en geef niet iedereen hetzelfde.
Oordeel over de bijdrage, niet over de vraag of iemand het met je eens was. Een beschuldiging of bekentenis verlaagt een score niet.
feedback: één zin in het Nederlands over wat goed was en wat beter kan.

Antwoord met alleen dit JSON-object:
{{"scores": {{"<rol>": {{"score": 1, "feedback": "..."}}}}}}"""


def _stem(van, naam, llm, rollen, debat):
    """De scores van één beoordelaar; een lege lijst als er geen geldig antwoord kwam."""
    te_scoren = [r for r in rollen if r != van]

    def keur(d):
        fouten = []
        for r in te_scoren:
            x = d["scores"].get(r)
            if not isinstance(x, dict):
                fouten.append(f"score voor {r} ontbreekt")
            elif type(x.get("score")) is not int or not 1 <= x["score"] <= 10:
                fouten.append(f"score van {r} moet een geheel getal van 1 tot 10 zijn")
            elif not is_tekst(x.get("feedback")):
                fouten.append(f"feedback voor {r} ontbreekt")
        return fouten

    if not te_scoren:
        return []
    d, fouten = llm._json(BEOORDEEL.format(wie=naam, wie_te_scoren=", ".join(te_scoren)), "Debat:\n\n" + debat, keur)
    if d is None or fouten:
        print(f"LET OP: geen geldige scores van {naam}: {'; '.join(fouten)}")
        return []
    return [{"van": van, "aan": r, "score": d["scores"][r]["score"], "feedback": d["scores"][r]["feedback"]} for r in te_scoren]


def geef_scores(beurten, leden, koning, klein):
    """Geeft {"gemiddelden": {rol: {"score", "aantal"}}, "stemmen": [...], "zonder_stem": [...]} terug.

    `koning` en `klein` zijn LLM's met `_json` (zie koning.Koning): de Koning op MODEL_GROOT, de Raadsleden op MODEL_KLEIN.
    Het gemiddelde weegt elke stem even zwaar, ook die van de Koning.
    """
    sprekers = {b["spreker"] for b in berichten(beurten)}
    rollen = [l.ROL for l in leden if l.ROL in sprekers]
    debat = tekst_debat(beurten)
    jury = [(l.ROL, f"het Raadslid {l.NAAM}", klein) for l in leden if l.ROL in sprekers] + [("koning", "de Koning", koning)]
    with ThreadPoolExecutor(max_workers=3) as pool:  # niet alles tegelijk: de key heeft een limiet per minuut
        lijsten = list(pool.map(lambda j: _stem(*j, rollen, debat), jury))
    stemmen = [s for lijst in lijsten for s in lijst]
    gemiddelden = {}
    for r in rollen:
        punten = [s["score"] for s in stemmen if s["aan"] == r]
        if punten:
            gemiddelden[r] = {"score": round(sum(punten) / len(punten), 1), "aantal": len(punten)}
    gaven = {s["van"] for s in stemmen}
    return {"gemiddelden": gemiddelden, "stemmen": stemmen, "zonder_stem": [van for van, _, _ in jury if van not in gaven and rollen != [van]]}
