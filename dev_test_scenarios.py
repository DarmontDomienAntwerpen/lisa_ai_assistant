"""Eenmalig testscript: laat Lisa een paar scenario's doorlopen en print het
resultaat. Bewust minimaal aantal beurten om kosten laag te houden. Niet
onderdeel van de productiecode.
"""
from __future__ import annotations

import asyncio

import conversation_store
import customer_lookup
import tenants
import usage_log
from agent import handle_turn
from config import close_pool, get_pool
from tenants import Tenant

TENANT = Tenant(
    client_id="dev_test",
    business_name="Kapsalon De Vries",
    niche="kapper",
    twilio_number="+3234000009",
    whatsapp_number="whatsapp:+3234000009",
    calendar_type="none",
    calendar_config={},
    system_prompt_extra="Wees warm en informeel, typisch Vlaams.",
    escalation_contact="+3247000009",
)


async def run_case(pool, label: str, phone: str, messages: list[str]) -> None:
    print(f"\n===== {label} (nummer: {phone}) =====")
    for msg in messages:
        print(f"Klant: {msg}")
        reply, escalated, booking_events = await handle_turn(TENANT, pool, phone, "terminal", msg)
        print(f"Lisa : {reply}")
        print(f"       [escalated={escalated}, booking_events={booking_events}]")


async def main() -> None:
    pool = await get_pool()
    await tenants.init_schema(pool)
    await customer_lookup.init_schema(pool)
    await conversation_store.init_schema(pool)
    await usage_log.init_schema(pool)
    await tenants.upsert_tenant(pool, TENANT)

    # Case 2 heeft een klant nodig die al bestaat maar nog geen gesprek heeft gehad
    await customer_lookup.local_create_customer(
        pool, TENANT.client_id, "+32488888882", {"name": "Sofie Peeters"}
    )

    await run_case(
        pool,
        "1. Nieuwe klant + boeking (alles in 1 zin)",
        "+32488888881",
        ["Hoi, ik wil graag donderdag om 14u een knipbeurt, ik heet Peter Janssens"],
    )

    await run_case(
        pool,
        "2. Bestaande klant, andere vraag (geen naam-check verwacht)",
        "+32488888882",
        ["Hoi, wat zijn jullie openingsuren op zaterdag?"],
    )

    await run_case(
        pool,
        "3. Klacht -> escalatie",
        "+32488888883",
        ["Ik heb een klacht, mijn vorige afspraak ging verkeerd en ik wil dringend met een mens spreken"],
    )

    await run_case(
        pool,
        "4. Afspraak annuleren (tenant heeft calendar_type=none -> geen bookings te vinden, test hallucinatierisico)",
        "+32488888881",  # zelfde klant als case 1, vervolggesprek
        ["Kan je mijn afspraak van donderdag annuleren?"],
    )

    await run_case(
        pool,
        "5. Buiten-scope / rare vraag",
        "+32488888884",
        ["Kan je me het wifi-wachtwoord van de zaak geven?"],
    )

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
