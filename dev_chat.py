"""Dev-tool: chat met Lisa in de terminal, zonder Twilio.

Gebruik: python dev_chat.py
Zet een test-tenant klaar (calendar_type="none") en laat je typen alsof je
whatsappt. Niet onderdeel van de productiecode — puur om lokaal te testen.
"""
from __future__ import annotations

import asyncio

from agent import handle_turn
from config import close_pool, get_pool
import conversation_store
import customer_lookup
import tenants
import usage_log
from tenants import Tenant

TEST_TENANT = Tenant(
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

TEST_PHONE_NUMBER = "+32499999999"


async def main() -> None:
    pool = await get_pool()
    await tenants.init_schema(pool)
    await customer_lookup.init_schema(pool)
    await conversation_store.init_schema(pool)
    await usage_log.init_schema(pool)
    await tenants.upsert_tenant(pool, TEST_TENANT)

    print(f"--- Chatten met Lisa ({TEST_TENANT.business_name}) — typ 'stop' om te stoppen ---")
    while True:
        user_message = input("Jij: ").strip()
        if user_message.lower() in {"stop", "exit", "quit"}:
            break
        if not user_message:
            continue
        reply, escalated, booking_events = await handle_turn(TEST_TENANT, pool, TEST_PHONE_NUMBER, "terminal", user_message)
        print(f"Lisa: {reply}")
        if escalated:
            print("      [escalatie: een medewerker zou nu verwittigd worden]")
        for event in booking_events:
            print(f"      [kapper genotificeerd: afspraak {event['type']}]")

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
