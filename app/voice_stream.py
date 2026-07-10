"""WebSocket-brug tussen Twilio Media Streams en de OpenAI Realtime-sessie.

Dunne laag: audio in/uit tussen Twilio en RealtimeConversation
(realtime_client.py). Alle beslissingen, tool-calling en transcript/usage-
logging leven daar — hier gebeurt uitsluitend audio-doorgifte en tenant-
opzoek bij het opzetten van de stream.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import WebSocket

from app import customer_lookup, tenants, usage_log
from app.config import get_pool
from app.escalation_email import send_escalation_email
from app.realtime_client import RealtimeConversation

logger = logging.getLogger("lisa")


async def _notify_escalation(
    tenant: tenants.Tenant, pool, caller_number: str, customer_name: str, escalation_type: str, reason: str
) -> None:
    if not customer_name:
        # Lisa vraagt intussen zelf om de naam voor ze escaleert als die nog
        # niet gekend was, maar val terug op een lookup voor bestaande
        # klanten waarvan de naam al in het systeem stond.
        customer, _ = await customer_lookup.find_or_flag_new(tenant, pool, caller_number)
        customer_name = (customer or {}).get("name", "")
    await send_escalation_email(tenant, caller_number, customer_name, escalation_type, reason)


async def run_media_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    pool = await get_pool()

    conversation: Optional[RealtimeConversation] = None
    relay_task: Optional[asyncio.Task] = None
    stream_sid = ""
    tenant: Optional[tenants.Tenant] = None
    caller_number = ""
    # Bijgehouden (niet enkel fire-and-forget) zodat we ze bij het opruimen
    # van de call kunnen afwachten — anders riskeert een snel afgesloten
    # gesprek dat de escalatiemail nooit verstuurd wordt (taak zonder
    # referentie kan door de garbage collector opgeruimd worden voor hij
    # klaar is, en het proces wacht er anders ook niet op).
    escalation_tasks: list[asyncio.Task] = []

    async def relay_to_twilio() -> None:
        assert conversation is not None
        async for event in conversation.events():
            if event["type"] == "audio_delta":
                await websocket.send_text(json.dumps({
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": event["payload"]},
                }))
            elif event["type"] == "interrupted":
                # Klant onderbrak Lisa: Twilio kan nog audio gebufferd hebben
                # van het vorige (nu afgekapte) antwoord — "clear" leegt die
                # buffer, anders speelt de staart van het oude antwoord nog
                # door bovenop het nieuwe.
                await websocket.send_text(json.dumps({"event": "clear", "streamSid": stream_sid}))
            elif event["type"] == "escalated":
                # Geen live call-transfer (vaak maar één zakennummer, dat al
                # naar Twilio doorschakelt — terugbellen zou in een lus
                # terechtkomen). In plaats daarvan: mail naar de eigenaar,
                # het gesprek met Lisa loopt gewoon door.
                assert tenant is not None
                escalation_tasks.append(
                    asyncio.create_task(
                        _notify_escalation(
                            tenant,
                            pool,
                            caller_number,
                            event.get("customer_name", ""),
                            event.get("escalation_type", "andere"),
                            event.get("reason", ""),
                        )
                    )
                )

    try:
        async for raw in websocket.iter_text():
            event = json.loads(raw)
            event_type = event.get("event")

            if event_type == "start":
                start = event["start"]
                stream_sid = start["streamSid"]
                params = start.get("customParameters", {})
                to_number = params.get("to_number", "")
                caller_number = params.get("caller_number", "")

                tenant = await tenants.get_tenant_by_number(pool, to_number)
                if tenant is None:
                    logger.warning("Media-stream gestart voor onbekend nummer: %s", to_number)
                    await websocket.close()
                    return

                conversation = RealtimeConversation(tenant, pool, caller_number, "voice", audio=True)
                await conversation.connect()
                relay_task = asyncio.create_task(relay_to_twilio())
                await conversation.start()

            elif event_type == "media" and conversation is not None:
                await conversation.send_audio_chunk(event["media"]["payload"])

            elif event_type == "stop":
                break
    finally:
        if relay_task is not None:
            relay_task.cancel()
        if conversation is not None:
            await conversation.close()
            # Voedt de Twilio-kostenschatting (duur × tarief) in het dashboard.
            await usage_log.log_call_end(pool, conversation.call_id)
        if escalation_tasks:
            # Wachten met een cap: de klant heeft al opgehangen op dit punt,
            # dus extra wachttijd hier raakt niemands gespreks-ervaring — maar
            # een hangende Gmail-call mag het opruimen van de WebSocket niet
            # eeuwig blokkeren.
            done, pending = await asyncio.wait(escalation_tasks, timeout=10)
            for task in pending:
                logger.error("Escalatiemail-taak nog niet klaar na 10s bij opruimen van de call — geannuleerd.")
                task.cancel()
