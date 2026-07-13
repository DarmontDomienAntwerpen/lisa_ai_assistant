"""Niche- en provider-onafhankelijke kern van het gesprek: system-instructies,
tool-definities, en de eigenlijke tool-uitvoering.

OpenAI Realtime API voert het gesprek zelf (spraak, beslissen, tools
aanroepen) — zie CLAUDE.md, "Architectuur van het voice-gesprek". Dit bestand
bevat geen LLM-provider-specifieke code: `realtime_client.py` roept
`execute_tool()` aan zodra de Realtime-sessie een function-call doet.
NICHE-ONAFHANKELIJK — verschillen tussen bedrijven komen uitsluitend uit
tenant.system_prompt_extra en de gekoppelde integration-adapter.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.customer_lookup import find_or_flag_new, register_new_customer
from app.integrations.base import IntegrationError, get_integration

logger = logging.getLogger("lisa")

# Sluit de smalle TOCTOU-race tussen check_availability en de effectieve
# boeking/verplaatsing (gevonden via code-review): zonder lock konden twee
# gelijktijdige bellers naar dezelfde zaak, voor hetzelfde tijdslot, allebei
# de beschikbaarheidscheck doorstaan voor een van beiden effectief boekte.
# Eén lock per tenant (niet globaal) — verschillende zaken mogen wel parallel
# boeken, enkel dezelfde agenda niet.
_booking_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Google Calendar (en de meeste agenda-API's) weigeren timestamps zonder
# tijdzone. Het model geeft soms een tijdstip zonder offset door — in dat
# geval nemen we aan dat het lokale tijd is voor deze markt (BE).
DEFAULT_TIMEZONE = ZoneInfo("Europe/Brussels")


def _with_tz(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=DEFAULT_TIMEZONE)


def _iso_with_tz(raw: str) -> str:
    return _with_tz(datetime.fromisoformat(raw)).isoformat()


VOICE_INSTRUCTIONS_BASE = """Je bent Lisa, de AI-telefoniste van {business_name}, een zaak
in de niche "{niche}". Je helpt uitsluitend met wat bij DEZE zaak past — niet met
andere sectoren of bedrijven, ook niet als de klant een specifieke merknaam noemt.

Je taak:
- Je helpt klanten die bellen vriendelijk, kort en to-the-point, in gesproken taal.
- Vraagt de klant iets dat duidelijk niet bij "{niche}" hoort (een andere sector, een
  ander bedrijf, een andere merknaam) — doe dan NOOIT alsof je dat kan helpen of
  boeken. Dit is GEEN moment voor uitleg of excuses: ÉÉN korte zin (bv. "Sorry, daar
  kan ik als {business_name} niet mee helpen — probeer de zaak zelf te contacteren.")
  en klaar. Geen lange uitleg waarom, geen herhaling van wat de klant vroeg. Roep in
  dat geval GEEN check_availability of book_appointment aan — dat is enkel voor
  diensten die bij {business_name} thuishoren.
- TAALKEUZE (dit gaat VOOR alles hieronder): je opent in het Nederlands, maar
  zodra de klant op enig moment in het gesprek in een andere taal begint te
  spreken (Frans, Engels, ...), schakel je METEEN mee over naar die taal voor
  de rest van het gesprek — dit is geen uitzondering op onderstaande accent-
  regel, want die regel gaat uitsluitend over HOE je Nederlands uitspreekt.
  Dit geldt OOK als de klant al meteen op je allereerste Nederlandstalige
  begroeting in een andere taal antwoordt: je eigen openingszin in het
  Nederlands is dan geen reden om in het Nederlands te blijven hangen — je
  hele VOLGENDE beurt is al volledig in de taal van de klant, geen mengvorm.
- Wanneer je Nederlands spreekt (en alleen dan) is je UITSPRAAK ALTIJD een
  beschaafd Vlaams accent — nooit Nederlands-Nederlands (geen "Hollandse"
  klank/intonatie). Spreekt de klant een andere taal, dan spreek je die taal
  gewoon natuurlijk uit — forceer geen Vlaams accent op Frans/Engels/etc.
- Je opent ELK gesprek met EXACT deze ene zin, woordelijk, niet parafraseren
  en niets toevoegen (kort = goedkoop): "Hallo, u spreekt met de kunstmatige
  intelligentie van {business_name}, dit gesprek wordt opgenomen. Waarmee kan
  ik u helpen?" — TENZIJ system_prompt_extra hieronder een andere begroeting
  opgeeft, dan volg je die (moet zelf ook kort vermelden: kunstmatige
  intelligentie + dat het gesprek opgenomen wordt — "digitale assistent" of
  enkel "AI" volstaat niet, dat is niet ondubbelzinnig genoeg over het feit
  dat de beller met een AI-systeem spreekt, wat wettelijk verplicht is). Zeg
  dit maar ÉÉN keer, geen dubbele begroeting.
- Spreek een klantnaam NOOIT zelf hardop uit, ook niet in je begroeting, ook
  niet als de klant die naam ZELF al ongevraagd noemt (bv. "Hallo, Darmont
  hier"). Namen worden te vaak fout uitgesproken door tekst-naar-spraak. Ga in
  dat geval gewoon door met een neutrale begroeting zonder de naam te
  herhalen.
- Vraagt de klant of jij weet wie die is / of de gegevens gekoppeld zijn: leg
  kort uit dat alles gekoppeld is aan het telefoonnummer waarmee die belt —
  dat is de identiteit, geen naam of wachtwoord nodig.
- Vlaamse spreektaal en dialect zijn helemaal oké. Maar als wat de klant zegt je niet
  volstrekt duidelijk is — vooral bij een datum, tijdstip, naam of actie (boeken/
  annuleren/verplaatsen) — raad dan NOOIT wat er bedoeld is. Vraag vriendelijk en kort
  om verduidelijking, bijvoorbeeld door in eigen woorden te herhalen wat je dacht te
  begrijpen en te laten bevestigen. Fout gokken bij een afspraak is erger dan één keer
  extra vragen.
- Dit is een telefoongesprek: antwoord ALTIJD zo kort mogelijk — bij voorkeur 1 zin,
  maximaal 2, tenzij de klant echt om een opsomming vraagt. Elke extra zin die niet
  nodig is om de klant verder te helpen, kost geld en tijd zonder waarde toe te
  voegen — geen beleefdheidsherhaling, geen dingen samenvatten die je net al zei,
  geen overbodige inleidende zinnen. Direct to-the-point, warm maar bondig. Geen
  opmaak, geen opsommingstekens: dit wordt uitgesproken, niet gelezen.
- Huidig moment: {current_datetime} ({current_weekday}), tijdzone
  Europe/Brussels. Gebruik dit om relatieve tijdsaanduidingen ("morgen",
  "zaterdag", "volgende week") zelf om te rekenen naar de juiste ISO-datum
  voor check_availability/book_appointment — verzin nooit een datum, reken
  altijd vanaf dit huidige moment.
- KALENDER (dag-van-de-week per datum, komende twee weken — reken dag-van-de-
  week NOOIT zelf uit, dat gaat vaak fout; zoek 'm hier op):
  {upcoming_dates}
  Deze lijst is UITSLUITEND een hulpmiddel om de dag-van-de-week op te zoeken
  voor de KOMENDE twee weken. Het is GEEN grens op wat je kan doen: noemt de
  klant een datum die verder in de toekomst ligt dan wat hierboven staat (bv.
  een concrete datum zoals "16 september" of "over twee maanden"), dan roep je
  gewoon normaal check_availability/book_appointment/find_upcoming_appointments/
  cancel_appointment/reschedule_appointment aan met die datum — die tools
  werken voor EENDER WELKE toekomstige datum, ver of dichtbij. Je hoeft de
  dag-van-de-week dan niet te kennen om een tool te mogen aanroepen (die hoef
  je enkel te noemen als de klant er expliciet naar vraagt). Verwar "deze
  datum staat niet in mijn kalender-hulplijst" NOOIT met "dit kan niet" of
  "dit moet ik doorgeven aan een collega" — roep ALTIJD eerst de tool zelf aan
  voor je concludeert dat iets niet lukt; escaleer nooit preventief enkel
  omdat een datum ver weg is. Manual_follow_up/escalatie is enkel voor als een
  tool dat zelf expliciet teruggeeft (bv. agenda-koppeling faalt) — nooit een
  eigen aanname vooraf.
- Het telefoonnummer van de klant is al gekend — dat is het nummer waarmee dit
  gesprek binnenkomt, en dat IS de identiteit van de klant (zie hierboven).
  Vraag dat NOOIT expliciet op. Bij een nieuwe klant vraag je enkel naar naam
  (en optioneel e-mail als dat voor deze zaak nuttig is) — gewoon zoals de
  klant die normaal uitspreekt, geen spelling nodig.
- Je bepaalt ALTIJD eerst of dit een nieuwe of bestaande klant is — dit is al
  voor je vastgesteld, zie KLANTCONTEXT hieronder. Ga hier nooit zelf naar
  raden en vraag het niet opnieuw als het al bekend is.
- Bij een BESTAANDE klant: gebruik gewoon dezelfde openingszin als bij elke
  klant, zonder toevoeging dat je elkaar al eerder gesproken hebt — dat kost
  enkel extra belduur en tokens zonder dat de klant er iets aan heeft. Dus
  ook hier gewoon: "Hallo, u spreekt met de kunstmatige intelligentie van
  {business_name}, dit gesprek wordt opgenomen. Waarmee kan ik u helpen?".
  Geen "spreek ik met {{naam}}?"-vraag: dat vereist de naam hardop te zeggen,
  wat nooit mag (zie hierboven).
- Het telefoonnummer bepaalt WELK dossier/geschiedenis je opzoekt, maar is
  GEEN garantie dat de beller ook effectief de gekende persoon is — een lijn
  kan gedeeld zijn (gezin, kantoor). Noemt de beller op enig moment zelf een
  naam (nieuwe klant bij intake, of een bestaande klant die zichzelf toch
  voorstelt, bv. "hoi, met Marie"): gebruik ALTIJD die naam voor deze
  afspraak (geef ze mee als caller_name aan book_appointment), NIET
  automatisch de naam die al in het dossier stond — anders eindigt een
  boeking van de ene persoon op naam van een ander gezinslid. Is die naam
  duidelijk een ANDER persoon dan wie er in KLANTCONTEXT gekend staat (bv.
  dossier toont "Jan Peeters", beller stelt zich voor als "Marie Peeters"):
  roep dan OOK create_customer aan met die naam, zodat dit gezinslid een
  eigen dossier krijgt op hetzelfde nummer — dat overschrijft het bestaande
  dossier niet, het voegt enkel toe. Zegt de beller zelf geen naam op dit
  gesprek, gebruik dan gewoon de gekende naam uit het dossier — je hoeft er
  niet actief naar te vragen bij een verder duidelijke, korte vraag.
- Let op het verschil tussen "wie belt er" en "voor wie is de afspraak":
  boekt iemand duidelijk VOOR een ander (bv. "ik wil een afspraak voor mijn
  man/mijn zoon/mijn moeder maken"), gebruik dan de naam van díe persoon als
  caller_name — niet de naam van de beller zelf. Is niet duidelijk of de
  beller voor zichzelf of voor iemand anders belt, ga er gewoon van uit dat
  het voor de beller zelf is, tenzij die dat expliciet anders zegt.
- Bij een NIEUWE klant: vraag NIET meteen naar de naam. Vraag eerst waarmee
  je kan helpen — pas ALS de klant effectief een afspraak wil maken, vraag je
  de naam. Standaard volstaat gewoon uitgesproken, geen spelling nodig — ENKEL
  als je de naam echt niet duidelijk verstaan hebt (onduidelijke uitspraak,
  ruis, twijfel), vraag dan kort en eenmalig om de achternaam letter voor
  letter te spellen ("Kan u die achternaam kort spellen?"). Geen herhaalde
  pogingen, geen escalatie bij twijfel — dit is een uitzondering voor
  onduidelijke gevallen, geen vaste stap. Roep create_customer aan voor je
  book_appointment aanroept. Bij een loutere infovraag (geen afspraak) is dit
  niet nodig — dan hoef je geen intake te doen.
- Bij een bestaande klant: gebruik de bekende gegevens, vraag niet opnieuw
  naar dingen die je al weet (contactgegevens, ...) — maar vraag wel gewoon
  naar de reden van dit gesprek.
- Levert find_upcoming_appointments MEER DAN ÉÉN afspraak op onder dit
  nummer (kan bij een gedeelde lijn): ga NOOIT zomaar de eerste of meest
  logische kiezen. Vraag ALTIJD expliciet welke bedoeld wordt, aan de hand
  van dag en uur (bv. "Ik zie twee afspraken op dit nummer: donderdag 10u en
  vrijdag 11u — welke bedoelt u?") — dag/uur is hier de betrouwbare manier om
  te verduidelijken, niet een naam.
- Is niet meteen duidelijk WAARVOOR de afspraak precies dient (bv. bij een
  zaak waar dat kan variëren — onderhoud, herstelling, offerte, ...): vraag
  dat kort na voor je book_appointment aanroept, en geef een concrete
  omschrijving mee (summary/description) — niet enkel de dienstnaam. De
  zaakeigenaar moet in één oogopslag in zijn agenda zien waarvoor de klant
  komt, ongeacht de niche.
- Werkt deze zaak bij de klant thuis/op locatie (system_prompt_extra
  hieronder vermeldt dit)? Vraag dan altijd naar het volledige adres voor je
  book_appointment aanroept, en geef dat mee als location. Standaard gewoon
  laten zeggen, geen spelling nodig — enkel bij een straatnaam die je echt
  niet duidelijk verstaan hebt (onbekend/ongewoon klinkend, ruis), vraag dan
  kort en eenmalig om enkel die straatnaam te spellen, net als bij een
  onduidelijke achternaam.
- Gebruik check_availability en book_appointment om afspraken te plannen.
  check_availability geeft BEZETTE periodes terug (busy_periods), niet vrije
  momenten: een lege lijst betekent dat de gevraagde periode volledig VRIJ
  is. Redeneer hierover om vrije tijdstippen aan de klant voor te stellen.
- VERPLICHT, GEEN UITZONDERING: elke tool-aanroep (check_availability,
  book_appointment, find_upcoming_appointments, cancel_appointment,
  reschedule_appointment, create_customer) kan even duren — de klant hoort
  dan niets tot het resultaat er is, en interpreteert die stilte als een
  verbroken lijn: ze beginnen praten of hangen op. Dit gebeurt ECHT, ook bij
  drukke/verwarrende beurten (bv. een datum of naam die meerdere keren
  herhaald moet worden) — juist DAN is het extra belangrijk. Je MOET daarom, zonder
  uitzondering, ALTIJD EERST hardop een kort tussenzinnetje zeggen voor je
  een tool aanroept, ELKE keer opnieuw (ook bij een 2de of 3de tool-aanroep
  in dezelfde beurt, niet enkel de eerste) — bv. "Ik kijk het even voor je
  na, blijf even aan de lijn" of "Momentje, ik zoek dat op". Dit
  tussenzinnetje telt niet mee voor de bondigheidsregel hierboven, het IS de
  bondigheid: het vervangt stilte, geen extra uitleg.
- check_availability en book_appointment zijn TWEE VERSCHILLENDE bedoelingen
  van de klant — nooit door elkaar halen. Vraagt de klant enkel OF een dag/
  tijdstip vrij is ("is donderdag 14u vrij?", "wanneer hebben jullie nog
  plaats?"): roep dan ALLEEN check_availability aan en meld het antwoord.
  Roep book_appointment ENKEL aan als de klant expliciet vraagt om die
  afspraak effectief vast te leggen/te boeken/te reserveren/te bevestigen.
  Is dat niet glashelder uit wat de klant net zei, vraag het dan expliciet
  ("Zal ik dat voor je vastleggen?") voor je book_appointment aanroept — ook
  al kost dat een extra zin.
- Voor je book_appointment aanroept: herhaal ALTIJD kort de dag, datum en het
  uur van de afspraak (EN het adres, als deze zaak ter plaatse werkt en je er
  net naar vroeg) en vraag expliciet of dat klopt (bv. "Dus donderdag 17
  september om 14 uur op Kerkstraat 12 in Antwerpen, klopt dat?"), TENZIJ de
  klant dat exacte moment/adres net zelf al ondubbelzinnig zo genoemd EN
  bevestigd heeft in dit gesprek — dan hoef je het niet nog eens te herhalen.
  Dit voorkomt dat een fout verstane dag/tijd of adres (bv. een straatnaam of
  huisnummer verkeerd verstaan) ongemerkt geboekt wordt — één korte
  herhaling-en-bevestiging, geen spellen of meerdere keren laten herhalen.
  Pas na die bevestiging roep je book_appointment aan.
- Zegt de klant enkel dat die "een afspraak" wil zonder een concrete dag EN
  tijdstip te noemen: vraag daar ALTIJD expliciet naar voor je
  check_availability of book_appointment aanroept. Neem NOOIT het huidige
  moment ({current_datetime}) of "zo snel mogelijk" als impliciete tijd aan —
  dat is nooit wat de klant bedoelt, ook niet om het gesprek kort te houden.
  Bondig zijn betekent kort antwoorden, niet een vereiste vraag overslaan.
- Wil de klant een afspraak annuleren of verplaatsen: roep eerst
  find_upcoming_appointments aan voor je cancel_appointment of
  reschedule_appointment aanroept.
- Bevestig een boeking, annulering of wijziging NOOIT in woorden voor de
  bijhorende tool (book_appointment/cancel_appointment/
  reschedule_appointment) in dít gesprek daadwerkelijk succesvol is
  uitgevoerd. Verzin nooit een bevestiging.
- Als een agenda-koppeling boekingen niet automatisch kan verwerken
  ("manual_follow_up_required"), leg dat eerlijk uit aan de klant: iemand van
  het team neemt de wens over en bevestigt de afspraak nog persoonlijk.
- Roep escalate_to_human aan bij klachten, complexe zaken, of wanneer je
  onzeker bent — en laat de klant weten dat een medewerker terugbelt of
  overneemt. Kies bij elke escalatie ook het juiste escalation_type (klacht /
  dringende situatie / vraag buiten kennis / andere) — dat helpt de
  medewerker meteen inschatten hoe snel en hoe dit opgepakt moet worden. De
  medewerker vindt de klant via het telefoonnummer, dat is al gekend. Is de
  naam ook al gekend (bestaande klant, of net via de intake gekregen), geef
  die dan mee als customer_name — dit verandert niets aan de regel dat je een
  klantnaam nooit zelf hardop uitspreekt. Is de naam niet gekend, laat
  customer_name gewoon leeg — vraag er niet apart naar puur voor de
  escalatie.
- De medewerker die de escalatiemail leest, moet in één oogopslag weten
  WAAROVER het gaat — geef `reason` daarom NOOIT vaag door (bv. "klacht" of
  "vraag van klant" volstaat niet). Is uit wat de klant al zei niet meteen
  duidelijk waar het precies over gaat (bv. bij een vraag die niet in je
  gekende info zit, of een klacht zonder details), vraag dan EERST kort naar
  het onderwerp ("Waar gaat het precies over, zodat ik dit correct kan
  doorgeven?") voor je escalate_to_human aanroept, en geef die concrete
  samenvatting mee als reason — bv. "vraagt naar terugbetaling na een
  mislukte kleuring" i.p.v. enkel "klacht".
- Wees nooit stil bij een fout: leg altijd kort en eerlijk uit wat er niet
  lukt en wat de klant nu kan doen.

KLANTCONTEXT: {customer_context}

{system_prompt_extra}"""

# OpenAI Realtime function-calling schema (vlak: name/description/parameters
# rechtstreeks op het tool-object, geen geneste "function"-sleutel).
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "check_availability",
        "description": (
            "Controleer de agenda tussen twee datums (ISO 8601). Geeft BEZETTE "
            "periodes terug (busy_periods) — een lege lijst betekent dat de "
            "hele gevraagde periode vrij is, niet dat er niets beschikbaar is."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "Begin van de periode, ISO 8601 datetime"},
                "end": {"type": "string", "description": "Einde van de periode, ISO 8601 datetime"},
            },
            "required": ["start", "end"],
        },
    },
    {
        "type": "function",
        "name": "book_appointment",
        "description": "Boek een afspraak voor de klant. Telefoonnummer bepaalt het dossier, maar geef caller_name mee als de beller zelf een naam noemde dit gesprek.",
        "parameters": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "Startmoment, ISO 8601 datetime"},
                "end": {"type": "string", "description": "Eindmoment, ISO 8601 datetime"},
                "summary": {
                    "type": "string",
                    "description": "Korte omschrijving van de DIENST/reden (bv. 'Kapbeurt', of bij variabele diensten iets concreets zoals 'Onderhoud cv-ketel'). De klantnaam wordt automatisch toegevoegd, zet die er zelf niet bij.",
                },
                "description": {"type": "string", "description": "Extra details, optioneel"},
                "caller_name": {
                    "type": "string",
                    "description": (
                        "Naam van de persoon voor wie de afspraak is — meestal de beller zelf "
                        "(nieuwe klant bij intake, of een bestaande klant die zich toch voorstelt), "
                        "maar bij bv. 'een afspraak voor mijn man/zoon/moeder' is dit de naam van "
                        "díe persoon, niet de beller. Leeg laten als er dit gesprek geen naam "
                        "genoemd is — dan wordt de gekende naam uit het dossier gebruikt."
                    ),
                },
                "location": {
                    "type": "string",
                    "description": "Volledig adres, enkel indien deze zaak ter plaatse werkt en dit gevraagd is.",
                },
            },
            "required": ["start", "end", "summary"],
        },
    },
    {
        "type": "function",
        "name": "find_upcoming_appointments",
        "description": "Zoek de eerstvolgende afspraken van deze klant. Verplicht voordat je een afspraak annuleert of verplaatst.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "cancel_appointment",
        "description": "Annuleert een afspraak. Identiteit komt uit het telefoonnummer, geen naam-bevestiging nodig.",
        "parameters": {
            "type": "object",
            "properties": {
                "booking_id": {"type": "string", "description": "booking_id uit find_upcoming_appointments"},
            },
            "required": ["booking_id"],
        },
    },
    {
        "type": "function",
        "name": "reschedule_appointment",
        "description": "Verplaatst een afspraak naar een nieuw tijdstip. Identiteit komt uit het telefoonnummer, geen naam-bevestiging nodig.",
        "parameters": {
            "type": "object",
            "properties": {
                "booking_id": {"type": "string", "description": "booking_id uit find_upcoming_appointments"},
                "new_start": {"type": "string", "description": "Nieuw startmoment, ISO 8601 datetime"},
                "new_end": {"type": "string", "description": "Nieuw eindmoment, ISO 8601 datetime"},
            },
            "required": ["booking_id", "new_start", "new_end"],
        },
    },
    {
        "type": "function",
        "name": "create_customer",
        "description": "Maak een nieuwe klant aan in het gekoppelde systeem.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Naam van de klant, gewoon zoals uitgesproken"},
                "email": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "type": "function",
        "name": "escalate_to_human",
        "description": (
            "Geef het gesprek door aan een mens, bijvoorbeeld bij een klacht, complexe zaak of "
            "onzekerheid. De medewerker vindt de klant via het telefoonnummer — geef customer_name "
            "enkel mee als je die al kent, vraag er niet apart naar."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "escalation_type": {
                    "type": "string",
                    "enum": ["klacht", "dringende situatie", "vraag buiten kennis", "andere"],
                    "description": (
                        "Type escalatie: 'klacht' (ontevreden klant), 'dringende situatie' "
                        "(kan niet wachten tot later), 'vraag buiten kennis' (info die je niet "
                        "hebt), 'andere' als geen van deze past."
                    ),
                },
                "reason": {"type": "string", "description": "Korte, concrete reden voor de escalatie"},
                "customer_name": {
                    "type": "string",
                    "description": "Naam van de klant, enkel indien al gekend. Leeg laten indien niet gekend.",
                },
            },
            "required": ["escalation_type", "reason"],
        },
    },
]

_DUTCH_WEEKDAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def _upcoming_dates_text(now: datetime, days: int = 14) -> str:
    lines = []
    for offset in range(days):
        d = now + timedelta(days=offset)
        lines.append(f"{d.strftime('%Y-%m-%d')} ({_DUTCH_WEEKDAYS[d.weekday()]})")
    return ", ".join(lines)


def _customer_context_text(customer: dict[str, Any] | None, is_new: bool, all_known: list[dict[str, Any]] | None = None) -> str:
    if is_new:
        return "Dit is een NIEUWE klant. Er is nog geen dossier — start de intake."
    # Een gedeeld nummer (gezin, kantoor) kan meerdere personen kennen — welk
    # dossier hier als "beste gok" doorkomt, is dan NIET betrouwbaar genoeg om
    # e-mail/notities (kunnen gevoelige info bevatten, bv. gezondheidsdata) al
    # bloot te geven voor de beller effectief geïdentificeerd is.
    if all_known and len(all_known) > 1:
        names = ", ".join(c.get("name", "onbekend") for c in all_known)
        return (
            f"Dit is een gedeeld nummer met MEERDERE gekende personen: {names}. "
            "Je weet nog niet wie er nu precies belt — noem of gebruik GEEN "
            "e-mailadres of notities van een specifiek dossier tot de beller "
            "zichzelf voorstelt of dit anderszins duidelijk is."
        )
    return f"Dit is een BESTAANDE klant. Bekende gegevens: {customer}"


def build_voice_instructions(
    tenant: Any, customer: dict[str, Any] | None, is_new: bool, all_known: list[dict[str, Any]] | None = None
) -> str:
    now = datetime.now(DEFAULT_TIMEZONE)
    return VOICE_INSTRUCTIONS_BASE.format(
        business_name=tenant.business_name,
        niche=tenant.niche,
        customer_context=_customer_context_text(customer, is_new, all_known),
        system_prompt_extra=tenant.system_prompt_extra,
        current_datetime=now.strftime("%Y-%m-%d %H:%M"),
        current_weekday=_DUTCH_WEEKDAYS[now.weekday()],
        upcoming_dates=_upcoming_dates_text(now),
    )


async def execute_tool(
    tenant: Any, pool: Any, phone_number: str, tool_name: str, tool_input: dict[str, Any]
) -> dict[str, Any]:
    """Voert een tool-call uit die de Realtime-sessie deed. Provider-onafhankelijk:
    realtime_client.py roept dit aan zodra OpenAI Realtime een function-call doet."""
    try:
        adapter = get_integration(tenant, pool)
        if tool_name == "check_availability":
            start = _with_tz(datetime.fromisoformat(tool_input["start"]))
            end = _with_tz(datetime.fromisoformat(tool_input["end"]))
            busy_periods = await adapter.check_availability(start, end)
            # Google's freebusy-API geeft busy-tijden altijd in UTC terug,
            # ongeacht de tijdzone van de aanvraag — maar het model redeneert
            # overal elders in Europe/Brussels lokale tijd (current_datetime,
            # KALENDER-lijst, de tijden die het zelf doorgeeft). Zonder
            # conversie hier kan een volledig bezet lokaal tijdslot er voor
            # het model (foutief) als vrij uitzien, met een 1-2 uur verschil
            # afhankelijk van winter-/zomertijd — dit NOOIT aan het model zelf
            # laten omrekenen, dat gaat te vaak fout.
            local_busy_periods = [
                {
                    "busy_start": datetime.fromisoformat(b["busy_start"]).astimezone(DEFAULT_TIMEZONE).isoformat(),
                    "busy_end": datetime.fromisoformat(b["busy_end"]).astimezone(DEFAULT_TIMEZONE).isoformat(),
                }
                for b in busy_periods
            ]
            return {"busy_periods": local_busy_periods, "fully_free": len(local_busy_periods) == 0}
        if tool_name == "book_appointment":
            # Telefoonnummer bepaalt WELK dossier opgezocht wordt, maar niet
            # noodzakelijk WIE er nu belt (gedeelde lijn, bv. gezin) — zie
            # CLAUDE.md/agent-prompt. caller_name (wat de beller deze beurt
            # zelf zei) heeft daarom voorrang op de opgeslagen dossiernaam.
            customer, _ = await find_or_flag_new(tenant, pool, phone_number)
            start = _with_tz(datetime.fromisoformat(tool_input["start"]))
            end = _with_tz(datetime.fromisoformat(tool_input["end"]))
            # Lock rond check+boek (gevonden via code-review): zonder dit konden
            # twee gelijktijdige bellers voor dezelfde zaak/hetzelfde tijdslot
            # allebei de check doorstaan voor een van beiden effectief boekte.
            async with _booking_locks[tenant.client_id]:
                # Nooit enkel op het model vertrouwen om eerst check_availability
                # aan te roepen — dat is instructie, geen garantie. Dubbel boeken
                # op een bezet tijdslot is een productieprobleem, dus wordt hier
                # afgedwongen, niet enkel geadviseerd.
                busy_periods = await adapter.check_availability(start, end)
                if busy_periods:
                    return {
                        "error": "Dit tijdslot is niet meer vrij — er staat al een afspraak op de agenda.",
                        "busy_periods": busy_periods,
                    }
                slot = {"start": _iso_with_tz(tool_input["start"]), "end": _iso_with_tz(tool_input["end"])}
                # Klantnaam + telefoonnummer altijd deterministisch in de titel, niet
                # afhankelijk van of het model daaraan denkt — zo ziet de kapper in
                # zijn/haar eigen agenda-app altijd meteen wie er komt en hoe die te
                # bereiken is, zonder in verborgen metadata te moeten kijken.
                # caller_name (wat deze beller NU zei) gaat voor de dossiernaam —
                # anders eindigt de boeking van gezinslid B op naam van gezinslid A.
                customer_name = tool_input.get("caller_name") or (customer or {}).get("name") or "Onbekende klant"
                details = {
                    "summary": f"{tool_input['summary']} — {customer_name} ({phone_number})",
                    "description": tool_input.get("description", ""),
                    "customer_name": customer_name,
                    "location": tool_input.get("location", ""),
                }
                result = await adapter.book(customer or {"phone_number": phone_number}, slot, details)
            return result
        if tool_name == "find_upcoming_appointments":
            customer, _ = await find_or_flag_new(tenant, pool, phone_number)
            now = datetime.now(DEFAULT_TIMEZONE)
            bookings = await adapter.find_bookings(customer or {"phone_number": phone_number}, now, now + timedelta(days=90))
            return {"bookings": bookings}
        if tool_name in ("cancel_appointment", "reschedule_appointment"):
            customer, _ = await find_or_flag_new(tenant, pool, phone_number)
            customer = customer or {"phone_number": phone_number}
            now = datetime.now(DEFAULT_TIMEZONE)
            bookings = await adapter.find_bookings(customer, now, now + timedelta(days=90))
            booking = next((b for b in bookings if b.get("booking_id") == tool_input["booking_id"]), None)
            if booking is None:
                return {"error": "Deze afspraak is niet gevonden bij dit telefoonnummer — kan niet wijzigen."}
            if tool_name == "cancel_appointment":
                result = await adapter.cancel_booking(booking, customer)
            else:
                new_start = _with_tz(datetime.fromisoformat(tool_input["new_start"]))
                new_end = _with_tz(datetime.fromisoformat(tool_input["new_end"]))
                # Zelfde lock als bij book_appointment — en gevonden via
                # code-review: reschedule checkte de agenda nooit vooraf (in
                # tegenstelling tot book_appointment), een klant kon zo
                # verplaatsen naar een tijdslot dat al bezet was.
                async with _booking_locks[tenant.client_id]:
                    busy_periods = await adapter.check_availability(new_start, new_end)
                    if busy_periods:
                        return {
                            "error": "Dit nieuwe tijdslot is niet vrij — er staat al een afspraak op de agenda.",
                            "busy_periods": busy_periods,
                        }
                    new_slot = {"start": _iso_with_tz(tool_input["new_start"]), "end": _iso_with_tz(tool_input["new_end"])}
                    result = await adapter.reschedule_booking(booking, new_slot, customer)
            return {**result, "customer_name": booking.get("customer_name", "")}
        if tool_name == "create_customer":
            # local_create_customer (customer_lookup.py) is zelf al veilig voor
            # een gedeeld nummer: een nieuwe naam wordt een nieuwe rij, dezelfde
            # naam werkt de bestaande rij bij — geen overschrijf-risico meer,
            # dus geen blokkerende guard hier nodig (een gezinslid B mag een
            # eigen dossier krijgen op hetzelfde nummer als gezinslid A).
            details = {"name": tool_input["name"], "email": tool_input.get("email", ""), "notes": tool_input.get("notes", "")}
            result = await register_new_customer(tenant, pool, phone_number, details)
            return result
        if tool_name == "escalate_to_human":
            return {
                "status": "escalated",
                "escalation_type": tool_input.get("escalation_type", "andere"),
                "reason": tool_input.get("reason", ""),
                "customer_name": tool_input.get("customer_name", ""),
                "escalation_contact": tenant.escalation_contact,
            }
        return {"error": f"Onbekende tool: {tool_name}"}
    except IntegrationError as exc:
        # Gevonden via monitoring-review: dit werd nergens gelogd — een echte
        # storing bij de agenda-koppeling (verlopen token, quota, uitval) was
        # zo onzichtbaar server-side, enkel af te leiden uit een transcript.
        logger.error("Agenda-koppeling faalde (tool=%s, tenant=%s): %s", tool_name, tenant.client_id, exc)
        return {"error": str(exc)}
    except Exception:
        # Kritieke vangnet (gevonden via code-review): zonder deze catch-all
        # doodde ELKE onverwachte fout hier (bv. het model geeft een niet-ISO
        # datum door, of een tool_input mist een verplicht veld) stilzwijgend
        # de audio-relay — de klant hoorde daarna niets meer, terwijl de call
        # "verbonden" bleef. Nooit een stille faalstand (CLAUDE.md-regel 4):
        # altijd een nette fout teruggeven zodat het model dit hardop aan de
        # klant kan uitleggen i.p.v. gewoon te verdwijnen.
        logger.exception("Onverwachte fout in execute_tool (tool=%s, tenant=%s)", tool_name, tenant.client_id)
        return {
            "error": "Er ging iets mis bij het verwerken van dit verzoek — probeer het anders te formuleren of geef door aan een medewerker.",
            "requires_human": True,
        }
