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

import tenants
from config import get_pool
from realtime_client import RealtimeConversation

logger = logging.getLogger("lisa")


async def run_media_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    pool = await get_pool()

    conversation: Optional[RealtimeConversation] = None
    relay_task: Optional[asyncio.Task] = None
    stream_sid = ""

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
