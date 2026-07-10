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

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.customer_lookup import find_or_flag_new, register_new_customer
from app.integrations.base import IntegrationError, get_integration

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
  en niets toevoegen (kort = goedkoop): "Hallo, u spreekt met de digitale
  assistent van {business_name}, dit gesprek wordt opgenomen. Waarmee kan ik
  u helpen?" — TENZIJ system_prompt_extra hieronder een andere begroeting
  opgeeft, dan volg je die (moet zelf ook kort vermelden: digitale/AI-
  assistent + dat het gesprek opgenomen wordt). Zeg dit maar ÉÉN keer, geen
  dubbele begroeting.
- Spreek een klantnaam NOOIT zelf hardop uit, ook niet in je begroeting, ook
  niet als de klant die naam ZELF al ongevraagd noemt (bv. "Hallo, Darmont
  hier"). Namen worden te vaak fout uitgesproken door tekst-naar-spraak. Ga in
  dat geval gewoon door met een neutrale begroeting zonder de naam te
  herhalen, en vraag alsnog om de achternaam te spellen zoals hieronder
  beschreven — ook al heeft de klant 'm net zelf uitgesproken.
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
  tool dat zelf expliciet teruggeeft (bv. agenda-koppeling faalt), of bij een
  echte naam-mismatch — nooit een eigen aanname vooraf.
- Het telefoonnummer van de klant is al gekend — dat is het nummer waarmee dit
  gesprek binnenkomt. Vraag dat NOOIT expliciet op. Bij een nieuwe klant vraag
  je enkel naar naam (en optioneel e-mail als dat voor deze zaak nuttig is).
- Je bepaalt ALTIJD eerst of dit een nieuwe of bestaande klant is — dit is al
  voor je vastgesteld, zie KLANTCONTEXT hieronder. Ga hier nooit zelf naar
  raden en vraag het niet opnieuw als het al bekend is.
- Bij een BESTAANDE klant: voeg aan het EINDE van je openingszin kort toe "We
  hebben elkaar al eerder gesproken" (zonder de naam te noemen) — dus:
  "Hallo, u spreekt met de digitale assistent van {business_name}, dit
  gesprek wordt opgenomen. We hebben elkaar al eerder gesproken — waarmee kan
  ik u helpen?". Geen "spreek ik met {{naam}}?"-vraag: dat vereist de naam
  hardop te zeggen, wat nooit mag (zie hierboven). De echte identiteitscheck
  gebeurt al verderop, via de gespelde achternaam-bevestiging vlak voor
  book_appointment/cancel_appointment/reschedule_appointment — dat is genoeg
  bescherming tegen een gedeeld telefoonnummer, ook zonder dit al meteen bij
  de opening te checken.
- Bij een NIEUWE klant: vraag NIET meteen naar de naam. Vraag eerst waarmee
  je kan helpen — pas ALS de klant effectief een afspraak wil maken, vraag je
  de achternaam (geen voornaam nodig), en laat de klant die letter voor
  letter spellen — namen worden te vaak verkeerd verstaan/getranscribeerd om
  zomaar aan te nemen. Roep pas daarna create_customer aan voor je
  book_appointment aanroept. Bij een loutere infovraag (geen afspraak) is dit
  niet nodig — dan hoef je geen intake te doen.
- Versta of hoor je die spelling niet goed (onduidelijke/dubbelzinnige
  letters, stilte, ruis): dit is GEEN reden om te escaleren. Zeg gewoon
  rustig en vriendelijk dat je het niet goed meekreeg en vraag om de
  achternaam nog eens, rustig, letter voor letter te spellen. Herhaal dit
  gerust een paar keer — pas als het na meerdere pogingen nog steeds niet
  lukt, escaleer je naar een medewerker in plaats van te blijven proberen of
  te gokken.
- Nadat de klant de achternaam gespeld heeft (nieuwe klant bij intake, of
  bestaande klant vlak voor een boeking/annulering/wijziging): herhaal ALTIJD
  eerst hardop welke naam je begrepen hebt (het samengevoegde woord, bv.
  "Dus dat is Peeters, klopt dat?") en wacht op bevestiging voor je verder
  gaat of een tool aanroept — nooit stilzwijgend aannemen dat de spelling
  goed verstaan is. Zegt de klant dat het niet klopt, laat dan opnieuw
  spellen (zie hierboven) en herhaal die bevestigingsstap opnieuw.
- Bij een bestaande klant: gebruik de bekende gegevens, vraag niet opnieuw
  naar dingen die je al weet (naam, contactgegevens, ...) — maar vraag wel
  gewoon naar de reden van dit gesprek.
- Gebruik check_availability en book_appointment om afspraken te plannen.
  check_availability geeft BEZETTE periodes terug (busy_periods), niet vrije
  momenten: een lege lijst betekent dat de gevraagde periode volledig VRIJ
  is. Redeneer hierover om vrije tijdstippen aan de klant voor te stellen.
- Elke tool-aanroep (check_availability, book_appointment,
  find_upcoming_appointments, cancel_appointment, reschedule_appointment,
  create_customer) kan even duren — de klant hoort dan niets tot het
  resultaat er is, en kan dat stilte-gaatje interpreteren als een verbroken
  lijn en beginnen praten of ophangen. Zeg daarom ALTIJD EERST hardop een
  kort, natuurlijk tussenzinnetje voor je de tool aanroept — nooit stil een
  tool aanroepen zonder dat de klant weet dat je iets aan het doen bent. Bv.
  "Ik kijk het even voor je na, blijf even aan de lijn" of "Momentje, ik zoek
  dat op". Dit tussenzinnetje telt niet mee voor de bondigheidsregel
  hierboven, het IS de bondigheid: het vervangt stilte, geen extra uitleg.
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
  uur van de afspraak en vraag expliciet of dat klopt (bv. "Dus donderdag 17
  september om 14 uur, klopt dat?"), TENZIJ de klant dat exacte moment net
  zelf al ondubbelzinnig zo genoemd EN bevestigd heeft in dit gesprek — dan
  hoef je het niet nog eens te herhalen. Dit voorkomt dat een fout verstane
  dag/tijd (bv. "donderdag" verstaan als "dinsdag") ongemerkt geboekt wordt.
  Pas na die bevestiging roep je book_appointment aan.
- Zegt de klant enkel dat die "een afspraak" wil zonder een concrete dag EN
  tijdstip te noemen: vraag daar ALTIJD expliciet naar voor je
  check_availability of book_appointment aanroept. Neem NOOIT het huidige
  moment ({current_datetime}) of "zo snel mogelijk" als impliciete tijd aan —
  dat is nooit wat de klant bedoelt, ook niet om het gesprek kort te houden.
  Bondig zijn betekent kort antwoorden, niet een vereiste vraag overslaan.
- Bij een BESTAANDE klant: vraag ALTIJD om bevestiging van de achternaam voor
  je book_appointment, cancel_appointment of reschedule_appointment aanroept
  — ook als je denkt de klant al te kennen — en laat die ook hier letter voor
  letter spellen. Geef die achternaam mee als confirmed_customer_name — als
  het GEWONE, samengevoegde woord (bv. "Peeters"), NOOIT als de losse letters
  met streepjes ertussen (dus NIET "P-E-E-T-E-R-S") — je vraagt om het
  letter voor letter te SPELLEN puur om de transcriptie te verifiëren, niet
  om die spelling-notatie zelf door te geven aan de tool. Dit voorkomt dat er
  iets fout gaat bij een gedeeld telefoonnummer (bv. gezinsleden) — al biedt
  de achternaam alleen minder bescherming dan een volledige naam als
  gezinsleden dezelfde achternaam delen. Bij een NIEUWE klant heb je de naam
  al net via de intake gekregen, dat volstaat voor book_appointment.
- Komt een tool (book_appointment/cancel_appointment/reschedule_appointment)
  terug met een fout over een naam die niet overeenkomt: ESCALEER NIET
  METEEN. Dit kan een verkeerde klant zijn, maar minstens even vaak gewoon
  een verkeerd verstane/getranscribeerde letter aan jouw kant. Zeg vriendelijk
  dat de naam niet overeenkwam en vraag de klant rustig om de achternaam nog
  eens, letter voor letter, te spellen — roep de tool dan opnieuw aan met de
  herbevestigde naam. Pas als het ook na dat hernieuwde spellen nog steeds
  niet overeenkomt, zeg dat eerlijk en escaleer naar een medewerker in plaats
  van het zelf te blijven forceren.
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
  overneemt. De medewerker moet weten WIE terug te bellen: is de naam van de
  klant nog niet gekend op dit punt in het gesprek (bv. een nieuwe klant die
  meteen escaleert zonder ooit een afspraak te willen boeken), vraag dan EERST
  naar de achternaam en laat die letter voor letter spellen — net zoals bij
  een boeking — voor je escalate_to_human aanroept, en geef die mee als
  customer_name. Is de naam al gekend (bestaande klant, of net via de intake
  gekregen), vraag dan niet opnieuw. Dit verandert niets aan de regel dat je
  een klantnaam nooit zelf hardop uitspreekt — je vráágt om de spelling en
  geeft die enkel door aan de tool, je herhaalt of gebruikt de naam zelf niet
  in wat je zegt.
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
        "description": (
            "Boek een afspraak voor de klant. Bij een BESTAANDE klant: geef "
            "confirmed_customer_name mee (de achternaam die de klant net zelf "
            "gespeld heeft) — zelfde reden als bij annuleren/verplaatsen, ter "
            "bescherming tegen een gedeeld telefoonnummer. Bij een NIEUWE "
            "klant mag je dit weglaten, de achternaam komt dan al uit de intake."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "Startmoment, ISO 8601 datetime"},
                "end": {"type": "string", "description": "Eindmoment, ISO 8601 datetime"},
                "summary": {
                    "type": "string",
                    "description": "Korte omschrijving van de DIENST (bv. 'Kapbeurt'). De klantnaam wordt automatisch toegevoegd, zet die er zelf niet bij.",
                },
                "description": {"type": "string", "description": "Extra details, optioneel"},
                "confirmed_customer_name": {
                    "type": "string",
                    "description": "Bij een bestaande klant: achternaam zoals de klant die zelf net gespeld heeft",
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
        "description": (
            "Annuleert een afspraak. Roep dit pas aan nadat de klant zijn/haar naam heeft "
            "bevestigd — geef die naam mee als confirmed_customer_name."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "booking_id": {"type": "string", "description": "booking_id uit find_upcoming_appointments"},
                "confirmed_customer_name": {"type": "string", "description": "Achternaam zoals de klant die zelf net gespeld heeft"},
            },
            "required": ["booking_id", "confirmed_customer_name"],
        },
    },
    {
        "type": "function",
        "name": "reschedule_appointment",
        "description": (
            "Verplaatst een afspraak naar een nieuw tijdstip. Roep dit pas aan nadat de klant "
            "zijn/haar naam heeft bevestigd — geef die naam mee als confirmed_customer_name."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "booking_id": {"type": "string", "description": "booking_id uit find_upcoming_appointments"},
                "new_start": {"type": "string", "description": "Nieuw startmoment, ISO 8601 datetime"},
                "new_end": {"type": "string", "description": "Nieuw eindmoment, ISO 8601 datetime"},
                "confirmed_customer_name": {"type": "string", "description": "Achternaam zoals de klant die zelf net gespeld heeft"},
            },
            "required": ["booking_id", "new_start", "new_end", "confirmed_customer_name"],
        },
    },
    {
        "type": "function",
        "name": "create_customer",
        "description": "Maak een nieuwe klant aan in het gekoppelde systeem.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Achternaam, zoals de klant die zelf letter voor letter heeft gespeld"},
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
            "onzekerheid. Is de naam van de klant nog niet gekend op dit punt, vraag die eerst "
            "(achternaam, letter voor letter gespeld) en geef ze mee als customer_name — de "
            "medewerker die terugbelt moet weten wie te bellen."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Korte reden voor de escalatie"},
                "customer_name": {
                    "type": "string",
                    "description": "Achternaam zoals de klant die zelf net gespeld heeft, indien nog niet gekend. Leeg laten als de naam al gekend was.",
                },
            },
            "required": ["reason"],
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


def _customer_context_text(customer: dict[str, Any] | None, is_new: bool) -> str:
    if is_new:
        return "Dit is een NIEUWE klant. Er is nog geen dossier — start de intake."
    return f"Dit is een BESTAANDE klant. Bekende gegevens: {customer}"


def build_voice_instructions(tenant: Any, customer: dict[str, Any] | None, is_new: bool) -> str:
    now = datetime.now(DEFAULT_TIMEZONE)
    return VOICE_INSTRUCTIONS_BASE.format(
        business_name=tenant.business_name,
        niche=tenant.niche,
        customer_context=_customer_context_text(customer, is_new),
        system_prompt_extra=tenant.system_prompt_extra,
        current_datetime=now.strftime("%Y-%m-%d %H:%M"),
        current_weekday=_DUTCH_WEEKDAYS[now.weekday()],
        upcoming_dates=_upcoming_dates_text(now),
    )


def _normalize_name(name: str) -> str:
    # Vergelijking moet ongevoelig zijn voor hoe de naam precies genoteerd is:
    # het model geeft confirmed_customer_name soms door als het losstaande,
    # met liggende streepjes gespelde woord (bv. "P-E-E-T-E-R-S") in plaats
    # van het samengevoegde woord ("Peeters") — allebei correct gespeld, maar
    # een kale stringvergelijking zou dat dan toch als mismatch zien. Enkel
    # alfanumerieke tekens, lowercase, telt mee voor de vergelijking.
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _name_mismatch(stored_name: str, claimed_name: str) -> bool:
    stored = _normalize_name(stored_name)
    claimed = _normalize_name(claimed_name)
    return not stored or stored != claimed


async def execute_tool(
    tenant: Any, pool: Any, phone_number: str, tool_name: str, tool_input: dict[str, Any], was_new_at_turn_start: bool = False
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
            # Vers opgevraagd (kan intussen net aangemaakt zijn door create_customer
            # in dezelfde beurt), maar de naam-check gebruikt was_new_at_turn_start:
            # was deze klant al bekend TOEN het gesprek begon? Zo niet, dan komt de
            # naam net uit de intake en hoeft ze niet opnieuw bevestigd te worden.
            customer, _ = await find_or_flag_new(tenant, pool, phone_number)
            if not was_new_at_turn_start and _name_mismatch(
                (customer or {}).get("name", ""), tool_input.get("confirmed_customer_name", "")
            ):
                return {
                    "error": "Naam komt niet overeen met de gekende klant. Kan dit niet automatisch verwerken — een medewerker moet de identiteit bevestigen.",
                    "requires_human": True,
                }
            start = _with_tz(datetime.fromisoformat(tool_input["start"]))
            end = _with_tz(datetime.fromisoformat(tool_input["end"]))
            # Nooit enkel op het model vertrouwen om eerst check_availability aan
            # te roepen — dat is instructie, geen garantie. Dubbel boeken op een
            # bezet tijdslot is een productieprobleem, dus wordt hier afgedwongen,
            # niet enkel geadviseerd.
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
            customer_name = (customer or {}).get("name") or tool_input.get("confirmed_customer_name") or "Onbekende klant"
            details = {
                "summary": f"{tool_input['summary']} — {customer_name} ({phone_number})",
                "description": tool_input.get("description", ""),
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
            if _name_mismatch(booking.get("customer_name", ""), tool_input["confirmed_customer_name"]):
                return {
                    "error": "Naam komt niet overeen met de afspraak. Kan dit niet automatisch verwerken — een medewerker moet de identiteit bevestigen.",
                    "requires_human": True,
                }
            if tool_name == "cancel_appointment":
                result = await adapter.cancel_booking(booking, customer)
            else:
                new_slot = {"start": _iso_with_tz(tool_input["new_start"]), "end": _iso_with_tz(tool_input["new_end"])}
                result = await adapter.reschedule_booking(booking, new_slot, customer)
            return {**result, "customer_name": booking.get("customer_name", "")}
        if tool_name == "create_customer":
            # Nooit enkel op het model vertrouwen om create_customer alleen voor
            # ECHT nieuwe klanten aan te roepen — als er al een dossier bestaat
            # voor dit nummer, zou create_customer dat stil OVERSCHRIJVEN
            # (local_create_customer doet ON CONFLICT DO UPDATE), wat de hele
            # naam-mismatch-bescherming omzeilt: een beller die een andere naam
            # opgeeft zou zo het bestaande dossier kunnen overschrijven vóór de
            # mismatch-check ooit een kans krijgt iets te weigeren.
            existing_customer, _ = await find_or_flag_new(tenant, pool, phone_number)
            if existing_customer is not None:
                return {
                    "error": "Er bestaat al een dossier voor dit nummer — kan geen nieuwe klant aanmaken. Een medewerker moet dit bevestigen.",
                    "requires_human": True,
                }
            details = {"name": tool_input["name"], "email": tool_input.get("email", ""), "notes": tool_input.get("notes", "")}
            result = await register_new_customer(tenant, pool, phone_number, details)
            return result
        if tool_name == "escalate_to_human":
            return {
                "status": "escalated",
                "reason": tool_input.get("reason", ""),
                "customer_name": tool_input.get("customer_name", ""),
                "escalation_contact": tenant.escalation_contact,
            }
        return {"error": f"Onbekende tool: {tool_name}"}
    except IntegrationError as exc:
        return {"error": str(exc)}
