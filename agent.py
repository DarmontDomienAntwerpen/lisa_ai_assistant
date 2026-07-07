"""Claude-logica: generieke system prompt, tool-definities, model-routing.

NICHE-ONAFHANKELIJK — geen niche-specifieke logica hier. Verschillen tussen
bedrijven komen uitsluitend uit tenant.system_prompt_extra en de gekoppelde
integration-adapter.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import anthropic

from config import ANTHROPIC_API_KEY, MAX_TOOL_ITERATIONS, MODEL_DEFAULT, MODEL_ESCALATION
from conversation_store import append_message, get_history
from customer_lookup import find_or_flag_new, register_new_customer
from integrations.base import IntegrationError, get_integration
from usage_log import log_usage

_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# Google Calendar (en de meeste agenda-API's) weigeren timestamps zonder
# tijdzone. Het model geeft soms een tijdstip zonder offset door — in dat
# geval nemen we aan dat het lokale tijd is voor deze markt (BE).
DEFAULT_TIMEZONE = ZoneInfo("Europe/Brussels")


def _with_tz(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=DEFAULT_TIMEZONE)


def _iso_with_tz(raw: str) -> str:
    return _with_tz(datetime.fromisoformat(raw)).isoformat()

SYSTEM_PROMPT_BASE = """Je bent Lisa, de AI-secretaresse van {business_name}.

Je taak:
- Je helpt klanten die bellen of whatsappen vriendelijk, kort en to-the-point.
- Je spreekt Nederlands, tenzij de klant in een andere taal schrijft.
- Vlaamse spreektaal en dialect zijn helemaal oké — daar hoef je niks van te
  zeggen. Maar als een bericht (dialect, tikfout, of gewoon onduidelijk
  geschreven) je niet volstrekt duidelijk is over WAT de klant precies
  bedoelt — vooral bij een datum, tijdstip, naam of actie (boeken/annuleren/
  verplaatsen) — raad dan NOOIT wat er bedoeld is. Vraag vriendelijk en kort
  om verduidelijking, bijvoorbeeld door in eigen woorden te herhalen wat je
  dacht te begrijpen en te laten bevestigen. Fout gokken bij een afspraak is
  erger dan één keer extra vragen.
- Dit is WhatsApp/telefonie, geen chat-app met opmaak: gebruik NOOIT
  markdown zoals **vet**, #kopjes of bullet-lijstjes met "- " — dat
  verschijnt letterlijk als tekens bij de klant. Gewone lopende zinnen,
  eventueel *woord* voor nadruk (WhatsApp-stijl, enkele sterretjes).
  Maximaal 2-3 korte zinnen per antwoord, tenzij de klant echt om een
  opsomming vraagt. Spaarzaam met emoji (hooguit één per bericht).
- Huidig moment: {current_datetime} ({current_weekday}), tijdzone
  Europe/Brussels. Gebruik dit om relatieve tijdsaanduidingen ("morgen",
  "zaterdag", "volgende week") zelf om te rekenen naar de juiste ISO-datum
  voor check_availability/book_appointment — verzin nooit een datum, reken
  altijd vanaf dit huidige moment.
- KALENDER (dag-van-de-week per datum, komende twee weken — reken dag-van-de-
  week NOOIT zelf uit, dat gaat vaak fout; zoek 'm hier op):
  {upcoming_dates}
- Het telefoonnummer van de klant is al gekend — dat is letterlijk het
  nummer waarmee dit gesprek binnenkomt (bellen of WhatsApp). Vraag dat
  NOOIT expliciet op. Bij een nieuwe klant vraag je enkel naar naam (en
  optioneel e-mail als dat voor deze zaak nuttig is).
- Je bepaalt ALTIJD eerst of dit een nieuwe of bestaande klant is — dit is al
  voor je vastgesteld, zie KLANTCONTEXT hieronder. Ga hier nooit zelf naar
  raden en vraag het niet opnieuw als het al bekend is.
- Ongeacht nieuw of bestaand: vraag ALTIJD eerst waarmee je kan helpen vandaag
  — dat weet je nooit vooraf, ook niet bij een bekende klant.
- Bij een nieuwe klant die een AFSPRAAK wil maken: vraag altijd voornaam ÉN
  achternaam (volledige naam), en roep daarna create_customer aan voor je
  book_appointment aanroept. Bij een loutere infovraag (geen afspraak) is dit
  niet nodig — dan hoef je geen intake te doen.
- Bij een bestaande klant: gebruik de bekende gegevens, vraag niet opnieuw
  naar dingen die je al weet (naam, contactgegevens, ...) — maar vraag wel
  gewoon naar de reden van dit gesprek.
- Gebruik check_availability en book_appointment om afspraken te plannen.
  check_availability geeft BEZETTE periodes terug (busy_periods), niet vrije
  momenten: een lege lijst betekent dat de gevraagde periode volledig VRIJ
  is. Redeneer hierover om vrije tijdstippen aan de klant voor te stellen.
- Bij een BESTAANDE klant: vraag ALTIJD om bevestiging van de naam voor je
  book_appointment, cancel_appointment of reschedule_appointment aanroept —
  ook als je denkt de klant al te kennen — en geef die naam mee als
  confirmed_customer_name. Dit voorkomt dat er iets fout gaat bij een gedeeld
  telefoonnummer (bv. gezinsleden). Bij een NIEUWE klant heb je de naam al
  net via de intake gekregen, dat volstaat voor book_appointment.
- Wil de klant een afspraak annuleren of verplaatsen: roep eerst
  find_upcoming_appointments aan voor je cancel_appointment of
  reschedule_appointment aanroept. Komt de opgegeven naam niet overeen met
  wat het systeem teruggeeft, zeg dat eerlijk en escaleer naar een
  medewerker in plaats van het zelf te forceren.
- Bevestig een boeking, annulering of wijziging NOOIT in woorden voor de
  bijhorende tool (book_appointment/cancel_appointment/
  reschedule_appointment) in dít gesprek daadwerkelijk succesvol is
  uitgevoerd. Verzin nooit een bevestiging.
- Als een agenda-koppeling boekingen niet automatisch kan verwerken
  ("manual_follow_up_required"), leg dat eerlijk uit aan de klant: iemand van
  het team neemt de wens over en bevestigt de afspraak nog persoonlijk.
- Roep escalate_to_human aan bij klachten, complexe zaken, of wanneer je
  onzeker bent — en laat de klant weten dat een medewerker overneemt.
- Wees nooit stil bij een fout: leg altijd kort en eerlijk uit wat er niet
  lukt en wat de klant nu kan doen.

KLANTCONTEXT: {customer_context}

{system_prompt_extra}"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "check_availability",
        "description": (
            "Controleer de agenda tussen twee datums (ISO 8601). Geeft BEZETTE "
            "periodes terug (busy_periods) — een lege lijst betekent dat de "
            "hele gevraagde periode vrij is, niet dat er niets beschikbaar is."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "Begin van de periode, ISO 8601 datetime"},
                "end": {"type": "string", "description": "Einde van de periode, ISO 8601 datetime"},
            },
            "required": ["start", "end"],
        },
    },
    {
        "name": "book_appointment",
        "description": (
            "Boek een afspraak voor de klant. Bij een BESTAANDE klant: geef "
            "confirmed_customer_name mee (de naam die de klant net zelf heeft "
            "bevestigd) — zelfde reden als bij annuleren/verplaatsen, ter "
            "bescherming tegen een gedeeld telefoonnummer. Bij een NIEUWE "
            "klant mag je dit weglaten, de naam komt dan al uit de intake."
        ),
        "input_schema": {
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
                    "description": "Bij een bestaande klant: naam zoals de klant die zelf net heeft bevestigd",
                },
            },
            "required": ["start", "end", "summary"],
        },
    },
    {
        "name": "find_upcoming_appointments",
        "description": "Zoek de eerstvolgende afspraken van deze klant. Verplicht voordat je een afspraak annuleert of verplaatst.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "cancel_appointment",
        "description": (
            "Annuleert een afspraak. Roep dit pas aan nadat de klant zijn/haar naam heeft "
            "bevestigd — geef die naam mee als confirmed_customer_name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "booking_id": {"type": "string", "description": "booking_id uit find_upcoming_appointments"},
                "confirmed_customer_name": {"type": "string", "description": "Naam zoals de klant die zelf net heeft opgegeven"},
            },
            "required": ["booking_id", "confirmed_customer_name"],
        },
    },
    {
        "name": "reschedule_appointment",
        "description": (
            "Verplaatst een afspraak naar een nieuw tijdstip. Roep dit pas aan nadat de klant "
            "zijn/haar naam heeft bevestigd — geef die naam mee als confirmed_customer_name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "booking_id": {"type": "string", "description": "booking_id uit find_upcoming_appointments"},
                "new_start": {"type": "string", "description": "Nieuw startmoment, ISO 8601 datetime"},
                "new_end": {"type": "string", "description": "Nieuw eindmoment, ISO 8601 datetime"},
                "confirmed_customer_name": {"type": "string", "description": "Naam zoals de klant die zelf net heeft opgegeven"},
            },
            "required": ["booking_id", "new_start", "new_end", "confirmed_customer_name"],
        },
    },
    {
        "name": "create_customer",
        "description": "Maak een nieuwe klant aan in het gekoppelde systeem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Volledige naam: voornaam én achternaam"},
                "email": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "escalate_to_human",
        "description": "Geef het gesprek door aan een mens, bijvoorbeeld bij een klacht, complexe zaak of onzekerheid.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Korte reden voor de escalatie"},
            },
            "required": ["reason"],
        },
    },
]

_ESCALATION_KEYWORDS = [
    "klacht", "klagen", "boos", "ontevreden", "slecht", "vervelend", "waardeloos",
    "mens", "iemand anders", "manager", "verantwoordelijke", "klantendienst",
    "dringend", "spoed", "advocaat", "terugbetaling",
    # Stammen i.p.v. volledige vervoegingen, zodat "annuleer"/"annuleert"/
    # "geannuleerd" allemaal matchen, niet enkel de infinitief "annuleren".
    # Annuleren/verplaatsen wijzigt een bestaande afspraak — dat vraagt om
    # het betrouwbaardere model (Sonnet maakt veel minder vaak de fout een
    # bevestiging te verzinnen zonder de tool ook echt aan te roepen).
    "annuleer", "verplaats", "verzet", "wijzig", "andere datum", "andere tijd",
    "kan niet komen", "kan niet meer",
]

# Hoeveel recente gespreksbeurten meetellen bij het kiezen van het model: een
# kort antwoord als "ja" of enkel een naam (bevestiging in een annuleer-/
# verplaats-flow) bevat zelf geen trefwoord, maar de vraag ervoor wel.
_MODEL_SELECTION_CONTEXT_MESSAGES = 4


def select_model(user_message: str, is_new: bool = True, recent_history: list[dict[str, Any]] | None = None) -> str:
    """Kiest het model voor dit gesprek. Haiku is de standaard; Sonnet bij
    complexe/gevoelige signalen (trefwoorden — enkel bruikbaar als vangnet
    voor NIEUWE klanten, want te broos om op te vertrouwen: Vlaamse spreektaal
    ("ik kan ni komen" i.p.v. "niet") mist trefwoorden zo vaak dat een
    annulering op Haiku bleef hangen zonder dat de tool ooit aangeroepen werd).

    Daarom: een BESTAANDE klant (is_new=False) krijgt altijd Sonnet, voor het
    hele gesprek — niet gebaseerd op trefwoorden in wat ze precies typen. Een
    bestaande klant kan een echte, al bestaande afspraak hebben; een verzonnen
    bevestiging daar is een productieprobleem, geen taalkundig ongemak. Dat
    weegt op tegen de hogere kost van Sonnet voor die klant."""
    if not is_new:
        return MODEL_ESCALATION
    recent_text = " ".join(
        m.get("content", "") if isinstance(m.get("content"), str) else ""
        for m in (recent_history or [])[-_MODEL_SELECTION_CONTEXT_MESSAGES:]
    )
    lowered = f"{recent_text} {user_message}".lower()
    if any(keyword in lowered for keyword in _ESCALATION_KEYWORDS):
        return MODEL_ESCALATION
    return MODEL_DEFAULT


def _customer_context_text(customer: dict[str, Any] | None, is_new: bool) -> str:
    if is_new:
        return "Dit is een NIEUWE klant. Er is nog geen dossier — start de intake."
    return f"Dit is een BESTAANDE klant. Bekende gegevens: {customer}"


_DUTCH_WEEKDAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def _upcoming_dates_text(now: datetime, days: int = 14) -> str:
    lines = []
    for offset in range(days):
        d = now + timedelta(days=offset)
        lines.append(f"{d.strftime('%Y-%m-%d')} ({_DUTCH_WEEKDAYS[d.weekday()]})")
    return ", ".join(lines)


def build_system_prompt(tenant: Any, customer: dict[str, Any] | None, is_new: bool) -> str:
    now = datetime.now(DEFAULT_TIMEZONE)
    return SYSTEM_PROMPT_BASE.format(
        business_name=tenant.business_name,
        customer_context=_customer_context_text(customer, is_new),
        system_prompt_extra=tenant.system_prompt_extra,
        current_datetime=now.strftime("%Y-%m-%d %H:%M"),
        current_weekday=_DUTCH_WEEKDAYS[now.weekday()],
        upcoming_dates=_upcoming_dates_text(now),
    )


def _name_mismatch(stored_name: str, claimed_name: str) -> bool:
    stored = (stored_name or "").strip().lower()
    claimed = (claimed_name or "").strip().lower()
    return not stored or stored != claimed


async def _execute_tool(
    tenant: Any, pool: Any, phone_number: str, tool_name: str, tool_input: dict[str, Any], was_new_at_turn_start: bool = False
) -> dict[str, Any]:
    try:
        adapter = get_integration(tenant, pool)
        if tool_name == "check_availability":
            start = _with_tz(datetime.fromisoformat(tool_input["start"]))
            end = _with_tz(datetime.fromisoformat(tool_input["end"]))
            busy_periods = await adapter.check_availability(start, end)
            return {"busy_periods": busy_periods, "fully_free": len(busy_periods) == 0}
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
            # Naam erbij zodat de kapper-notificatie (main.py) altijd weet WIE
            # er annuleerde/verplaatste, niet enkel een telefoonnummer/booking-id.
            return {**result, "customer_name": booking.get("customer_name", "")}
        if tool_name == "create_customer":
            details = {"name": tool_input["name"], "email": tool_input.get("email", ""), "notes": tool_input.get("notes", "")}
            result = await register_new_customer(tenant, pool, phone_number, details)
            return result
        if tool_name == "escalate_to_human":
            return {"status": "escalated", "reason": tool_input.get("reason", ""), "escalation_contact": tenant.escalation_contact}
        return {"error": f"Onbekende tool: {tool_name}"}
    except IntegrationError as exc:
        return {"error": str(exc)}


async def handle_turn(
    tenant: Any, pool: Any, phone_number: str, channel: str, user_message: str
) -> tuple[str, bool, list[dict[str, Any]]]:
    """Verwerkt één binnenkomend bericht (voice-transcript of WhatsApp) en
    geeft (Lisa's antwoord, escalated, booking_events) terug. Persisteert
    historie en usage-logging. main.py gebruikt escalated om
    escalation_contact te verwittigen bij een conversatie-overname, en
    booking_events om diezelfde contactpersoon een aparte melding te sturen
    bij elke succesvolle annulatie/verplaatsing (los van escalatie — de
    kapper moet dit weten, ook al hoeft die het gesprek niet over te nemen)."""
    customer, is_new = await find_or_flag_new(tenant, pool, phone_number)
    system_prompt = build_system_prompt(tenant, customer, is_new)

    history = await get_history(pool, tenant.client_id, phone_number)
    model = select_model(user_message, is_new, history)
    await append_message(pool, tenant.client_id, phone_number, channel, "user", user_message)

    messages: list[dict[str, Any]] = [*history, {"role": "user", "content": user_message}]

    total_input_tokens = 0
    total_output_tokens = 0
    escalated = False
    booking_events: list[dict[str, Any]] = []
    final_text = ""

    for _ in range(MAX_TOOL_ITERATIONS):
        try:
            response = await _client.messages.create(
                model=model,
                max_tokens=1024,
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            )
        except anthropic.APIError:
            final_text = "Sorry, ik heb momenteel een technisch probleempje. Een medewerker neemt zo snel mogelijk contact met je op."
            escalated = True
            break
        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens

        if response.stop_reason != "tool_use":
            final_text = "".join(block.text for block in response.content if block.type == "text")
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if block.name == "escalate_to_human":
                escalated = True
            result = await _execute_tool(tenant, pool, phone_number, block.name, block.input, is_new)
            if result.get("requires_human"):
                escalated = True
            if block.name in ("cancel_appointment", "reschedule_appointment") and result.get("status") in (
                "cancelled",
                "rescheduled",
            ):
                booking_events.append({"type": result["status"], **result})
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(result)})
        messages.append({"role": "user", "content": tool_results})
    else:
        final_text = "Sorry, dit gesprek vraagt om een menselijke blik — een medewerker neemt zo snel mogelijk contact met je op."
        escalated = True

    if not final_text:
        final_text = "Sorry, daar kwam ik even niet uit — een medewerker neemt dit met je op."
        escalated = True

    await append_message(pool, tenant.client_id, phone_number, channel, "assistant", final_text)
    await log_usage(pool, tenant.client_id, phone_number, channel, model, total_input_tokens, total_output_tokens, escalated)

    return final_text, escalated, booking_events
