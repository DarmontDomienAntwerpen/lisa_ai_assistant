"""Dev-tool: chat met Lisa in de terminal, zonder Twilio.

Gebruik: python dev_chat.py
Zet een test-tenant klaar (calendar_type="none") en praat in tekst met exact
dezelfde OpenAI Realtime-sessie/tool-laag als een echte oproep (audio=False
i.p.v. audio=True) — zie realtime_client.RealtimeConversation. Niet
onderdeel van de productiecode — puur om lokaal te testen.
"""
from __future__ import annotations

import asyncio

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import conversation_store, customer_lookup, tenants, usage_log  # noqa: E402
from app.config import close_pool, get_pool  # noqa: E402
from app.realtime_client import RealtimeConversation  # noqa: E402
from app.tenants import Tenant  # noqa: E402

TEST_TENANT = Tenant(
    client_id="dev_test",
    business_name="Kapsalon De Vries",
    niche="kapper",
    twilio_number="+3234000009",
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

    conversation = RealtimeConversation(TEST_TENANT, pool, TEST_PHONE_NUMBER, "terminal", audio=False)
    await conversation.connect()

    reply_ready = asyncio.Event()

    async def print_events() -> None:
        async for event in conversation.events():
            if event["type"] == "assistant_text":
                print(f"Lisa: {event['text']}")
                reply_ready.set()
            elif event["type"] == "escalated":
                print("      [escalatie: een medewerker zou nu verwittigd worden]")
            elif event["type"] == "booking_event":
                print(f"      [kapper genotificeerd: afspraak {event['status']}]")

    listener = asyncio.create_task(print_events())

    print(f"--- Chatten met Lisa ({TEST_TENANT.business_name}) — typ 'stop' om te stoppen ---")
    await conversation.start()
    await reply_ready.wait()
    reply_ready.clear()

    try:
        while True:
            user_message = (await asyncio.to_thread(input, "Jij: ")).strip()
            if user_message.lower() in {"stop", "exit", "quit"}:
                break
            if not user_message:
                continue
            await conversation.send_text(user_message)
            await reply_ready.wait()
            reply_ready.clear()
    finally:
        listener.cancel()
        await conversation.close()
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
