"""WebSocket bridge between Twilio Media Streams and the OpenAI Realtime session.

Thin layer: audio in/out between Twilio and RealtimeConversation
(realtime_client.py). All decisions, tool calling and transcript/usage
logging live there - here only audio is passed through and the tenant is
looked up when the stream is set up.
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
        # Lisa now asks for the name herself before escalating if it was
        # not known yet, but fall back on a lookup for existing customers
        # whose name was already in the system.
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
    # Tracked (not just fire-and-forget) so we can await them when the
    # call is cleaned up - otherwise a quickly closed conversation risks
    # the escalation email never being sent (a task without a reference
    # can be garbage-collected before it finishes, and the process would
    # not wait for it either).
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
                # The customer interrupted Lisa: Twilio may still have audio
                # buffered from the previous (now cut-off) answer - "clear"
                # empties that buffer, otherwise the tail of the old answer
                # keeps playing over the new one.
                await websocket.send_text(json.dumps({"event": "clear", "streamSid": stream_sid}))
            elif event["type"] == "escalated":
                # No live call transfer (often only one business number,
                # already forwarded to Twilio - calling back would end up in
                # a loop). Instead: email the owner; the conversation with
                # Lisa simply continues.
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
            # Feeds the Twilio cost estimate (duration x rate) in the dashboard.
            await usage_log.log_call_end(pool, conversation.call_id)
        try:
            # Crucial: without an explicit, clean WebSocket close here,
            # Twilio cannot finish the stream teardown properly (seen as
            # Twilio error 31921 "Stream - WebSocket - Close Error") - the
            # conversation itself was long over, but Twilio kept the line
            # "connected" (and billed) for ~13 more minutes until an internal
            # timeout, because our side never closed cleanly.
            await websocket.close()
        except RuntimeError:
            pass  # already closed (e.g. the client dropped the connection first)
        if escalation_tasks:
            # Wait with a cap: the customer has already hung up at this point,
            # so extra waiting here affects nobody's call experience - but a
            # hanging Gmail call must not block the WebSocket cleanup
            # forever.
            done, pending = await asyncio.wait(escalation_tasks, timeout=10)
            for task in pending:
                logger.error("Escalatiemail-taak nog niet klaar na 10s bij opruimen van de call — geannuleerd.")
                task.cancel()
