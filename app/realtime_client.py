"""Eén OpenAI Realtime-sessie: sessie opzetten (tools + instructies),
function-calls afhandelen via agent.execute_tool, en transcript/usage
capteren.

Audio (Twilio, voice_stream.py) en tekst (dev-tools) delen deze kern — enkel
de modaliteit en de manier waarop audio/tekst binnenkomt of buiten gaat
verschilt per aanroeper. Zo raakt lokaal testen exact dezelfde brain/tool-laag
als een echte oproep (zie CLAUDE.md, "Architectuur van het voice-gesprek").
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Optional

import websockets

from app import agent, conversation_store, usage_log
from app.config import (
    MAX_TOOL_ITERATIONS,
    OPENAI_API_KEY,
    OPENAI_REALTIME_MODEL,
    OPENAI_REALTIME_SPEED,
    OPENAI_REALTIME_VOICE,
)
from app.customer_lookup import find_or_flag_new

logger = logging.getLogger("lisa")

_OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"

# GA-schema audio-formats (geverifieerd tegen de echte API — dit is geen beta-
# schema meer: "g711_ulaw"/"pcm16" als platte strings bestaan niet meer).
_AUDIO_FORMATS: dict[str, dict[str, Any]] = {
    "g711_ulaw": {"type": "audio/pcmu"},  # Twilio Media Streams
    "pcm16": {"type": "audio/pcm", "rate": 24000},  # browser-mic dev-tool
}


class RealtimeConversation:
    """Eén live gesprek (call of dev-testgesprek) met de OpenAI Realtime API."""

    def __init__(self, tenant: Any, pool: Any, phone_number: str, channel: str, audio: bool, audio_format: str = "g711_ulaw"):
        self.tenant = tenant
        self.pool = pool
        self.phone_number = phone_number
        self.channel = channel
        self.audio = audio
        self.audio_format = audio_format  # "g711_ulaw" (Twilio) of "pcm16" (browser-mic dev-tool)
        self.escalated = False
        self._was_new_at_start = False
        self._tool_calls_this_turn = 0
        self._had_function_call_this_response = False
        # Voor onderbrekingen (barge-in): bij een WebSocket-verbinding (i.t.t.
        # WebRTC) is de CLIENT zelf verantwoordelijk voor het opschonen van een
        # lopende audio-respons zodra de klant begint te praten — anders speelt
        # de staart van het oude antwoord nog door bovenop het nieuwe, wat
        # klinkt als een stem die abrupt verandert. We houden bij welk
        # output-item nu audio streamt en sinds wanneer, om bij een interrupt
        # een zo goed mogelijke conversation.item.truncate te kunnen sturen.
        self._current_output_item_id: Optional[str] = None
        self._current_output_audio_started_at: Optional[float] = None
        self._ws: Optional[Any] = None
        self.call_id: Optional[int] = None

    async def connect(self) -> None:
        self.call_id = await usage_log.log_call_start(self.pool, self.tenant.client_id, self.phone_number, self.channel)
        customer, is_new = await find_or_flag_new(self.tenant, self.pool, self.phone_number)
        self._was_new_at_start = is_new
        instructions = agent.build_voice_instructions(self.tenant, customer, is_new)

        self._ws = await websockets.connect(
            f"{_OPENAI_REALTIME_URL}?model={OPENAI_REALTIME_MODEL}",
            additional_headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        )
        session: dict[str, Any] = {
            "type": "realtime",
            "model": OPENAI_REALTIME_MODEL,
            "instructions": instructions,
            "output_modalities": ["audio"] if self.audio else ["text"],
            "tools": agent.TOOLS,
            "tool_choice": "auto",
        }
        if self.audio:
            audio_format = _AUDIO_FORMATS[self.audio_format]
            session["audio"] = {
                "input": {
                    "format": audio_format,
                    # "prompt" biast de transcriptie naar aannemelijke vocabulaire
                    # voor DEZE zaak (bv. "knipbeurt" i.p.v. het foneetisch
                    # gelijkaardige "kniebeurt") — generiek via tenant.niche/
                    # business_name, geen hardcoded woordenlijst per niche.
                    "transcription": {
                        "model": "gpt-4o-mini-transcribe",
                        "prompt": f"Telefoongesprek met {self.tenant.business_name}, een zaak in de niche '{self.tenant.niche}'. Vlaams-Nederlands.",
                    },
                    # semantic_vad i.p.v. server_vad: beoordeelt of de zin van de
                    # klant semantisch/grammaticaal AF aanvoelt in plaats van enkel
                    # op een vaste stilte-timer te varen — een denkpauze midden in
                    # een zin ("ik ben... eh... Peeters") triggert dan geen voortijdig
                    # antwoord. eagerness="low" bleek te ver doorschieten (soms
                    # helemaal geen antwoord tot de klant nog eens iets zei) —
                    # "auto" is de gebalanceerde middenweg.
                    "turn_detection": {"type": "semantic_vad", "eagerness": "auto", "create_response": True},
                },
                "output": {"voice": OPENAI_REALTIME_VOICE, "format": audio_format, "speed": OPENAI_REALTIME_SPEED},
            }
        await self._ws.send(json.dumps({"type": "session.update", "session": session}))

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def start(self) -> None:
        """Lisa opent het gesprek zelf: geen klant-input nodig, gewoon vragen om
        een antwoord — ze groet meteen volgens haar instructies."""
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def send_audio_chunk(self, base64_payload: str) -> None:
        await self._ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64_payload}))

    async def send_text(self, text: str) -> None:
        await conversation_store.append_message(self.pool, self.tenant.client_id, self.phone_number, self.channel, "user", text)
        self._tool_calls_this_turn = 0
        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]},
        }))
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Normaliseert OpenAI Realtime-events voor de aanroeper. Function-calls
        worden hier al intern afgehandeld (agent.execute_tool) — de aanroeper
        ziet enkel audio/tekst-output en statusevents."""
        async for raw in self._ws:
            event = json.loads(raw)
            event_type = event.get("type")

            if event_type == "input_audio_buffer.speech_started":
                self._tool_calls_this_turn = 0
                interrupt_event = await self._handle_possible_interrupt()
                if interrupt_event:
                    yield interrupt_event

            elif event_type == "conversation.item.input_audio_transcription.completed":
                transcript = (event.get("transcript") or "").strip()
                if transcript:
                    await conversation_store.append_message(
                        self.pool, self.tenant.client_id, self.phone_number, self.channel, "user", transcript
                    )
                    yield {"type": "user_transcript", "text": transcript}

            elif event_type == "response.output_audio.delta":
                item_id = event.get("item_id")
                if item_id != self._current_output_item_id:
                    self._current_output_item_id = item_id
                    self._current_output_audio_started_at = time.monotonic()
                yield {"type": "audio_delta", "payload": event["delta"]}

            elif event_type in ("response.output_audio_transcript.done", "response.output_text.done"):
                text = event.get("transcript") or event.get("text") or ""
                if text:
                    await conversation_store.append_message(
                        self.pool, self.tenant.client_id, self.phone_number, self.channel, "assistant", text
                    )
                    yield {"type": "assistant_text", "text": text}

            elif event_type == "response.output_item.done":
                item = event.get("item", {})
                if item.get("type") == "function_call":
                    tool_event = await self._handle_function_call(item)
                    if tool_event:
                        yield tool_event

            elif event_type == "response.done":
                # Deze respons is normaal afgerond (niet onderbroken) — geen
                # actief output-item meer om bij een volgende speech_started
                # te moeten afkappen.
                self._current_output_item_id = None
                self._current_output_audio_started_at = None
                usage = (event.get("response") or {}).get("usage") or {}
                cached_input_tokens = (usage.get("input_token_details") or {}).get("cached_tokens", 0)
                await usage_log.log_usage(
                    self.pool,
                    self.tenant.client_id,
                    self.phone_number,
                    self.channel,
                    OPENAI_REALTIME_MODEL,
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                    self.escalated,
                    cached_input_tokens=cached_input_tokens,
                    call_id=self.call_id,
                )
                # Eén response kan MEERDERE function-calls bevatten (parallelle
                # tool-calls). Pas hier, na response.done, één response.create
                # sturen om verder te gaan — niet per afgehandelde tool-call
                # (zie _handle_function_call): anders start elke tool-call zijn
                # eigen nieuwe response, en die audio-streams overlappen elkaar
                # in de afspeel-wachtrij — dat klinkt als een stem/accent die
                # midden in een zin omslaat, want het zijn letterlijk twee
                # verschillende generaties die door elkaar gesplitst worden.
                if self._had_function_call_this_response:
                    self._had_function_call_this_response = False
                    await self._ws.send(json.dumps({"type": "response.create"}))

            elif event_type == "error":
                logger.error("OpenAI Realtime-fout voor tenant %s: %s", self.tenant.client_id, event.get("error"))
                yield {"type": "error", "error": event.get("error")}

    async def _handle_possible_interrupt(self) -> Optional[dict[str, Any]]:
        """De klant begint te praten. Als er nog een audio-respons aan het
        streamen was, is dat een onderbreking (barge-in): bij een WebSocket-
        verbinding moet de client zelf conversation.item.truncate sturen zodat
        OpenAI's eigen gespreksgeschiedenis niet denkt dat de klant de volledige
        (nooit-afgespeelde) rest van dat antwoord gehoord heeft. We geven ook
        een {"type": "interrupted"} event terug zodat de aanroeper (Twilio-
        brug/browser-tool) de EIGEN afspeel-wachtrij kan legen — anders blijft
        de staart van het oude antwoord nog doorspelen bovenop het nieuwe."""
        if self._current_output_item_id is None or self._current_output_audio_started_at is None:
            return None

        audio_end_ms = int((time.monotonic() - self._current_output_audio_started_at) * 1000)
        item_id = self._current_output_item_id
        self._current_output_item_id = None
        self._current_output_audio_started_at = None

        await self._ws.send(json.dumps({
            "type": "conversation.item.truncate",
            "item_id": item_id,
            "content_index": 0,
            "audio_end_ms": max(audio_end_ms, 0),
        }))
        return {"type": "interrupted"}

    async def _handle_function_call(self, item: dict[str, Any]) -> Optional[dict[str, Any]]:
        call_id = item["call_id"]
        name = item["name"]
        try:
            tool_input = json.loads(item.get("arguments") or "{}")
        except ValueError:
            tool_input = {}

        self._had_function_call_this_response = True
        self._tool_calls_this_turn += 1
        if self._tool_calls_this_turn > MAX_TOOL_ITERATIONS:
            result: dict[str, Any] = {
                "error": "Te veel achtereenvolgende tool-aanroepen in dit gesprek — een medewerker moet dit overnemen.",
                "requires_human": True,
            }
        else:
            result = await agent.execute_tool(
                self.tenant, self.pool, self.phone_number, name, tool_input, self._was_new_at_start
            )

        tool_event: Optional[dict[str, Any]] = None
        was_escalated = self.escalated
        if name == "escalate_to_human" or result.get("requires_human"):
            self.escalated = True
            if not was_escalated:
                logger.info("Gesprek geëscaleerd voor tenant %s / %s", self.tenant.client_id, self.phone_number)
                tool_event = {
                    "type": "escalated",
                    "escalation_type": tool_input.get("escalation_type", "andere") if name == "escalate_to_human" else "andere",
                    "reason": tool_input.get("reason", ""),
                    "customer_name": tool_input.get("customer_name", "") if name == "escalate_to_human" else "",
                }
        elif name in ("cancel_appointment", "reschedule_appointment") and result.get("status") in ("cancelled", "rescheduled"):
            tool_event = {"type": "booking_event", "status": result["status"], "customer_name": result.get("customer_name", "")}

        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id, "output": json.dumps(result)},
        }))
        # Geen response.create hier — zie response.done hierboven: dat gebeurt
        # precies één keer per response, ook als deze response meerdere
        # function-calls bevatte.
        return tool_event
