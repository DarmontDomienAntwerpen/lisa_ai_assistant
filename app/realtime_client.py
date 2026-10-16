"""One OpenAI Realtime session: set up the session (tools + instructions),
handle function calls via agent.execute_tool, and capture transcript/usage.

Audio (Twilio, voice_stream.py) and text (dev tools) share this core - only
the modality and the way audio/text comes in or goes out differ per caller.
So local testing hits exactly the same brain/tool layer as a real call (see
CLAUDE.md, "Architecture of the voice conversation").
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
from app.customer_lookup import find_or_flag_new, local_list_customers

logger = logging.getLogger("lisa")

_OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"

# GA-schema audio formats (verified against the real API - this is no longer
# the beta schema: "g711_ulaw"/"pcm16" as plain strings no longer exist).
_AUDIO_FORMATS: dict[str, dict[str, Any]] = {
    "g711_ulaw": {"type": "audio/pcmu"},  # Twilio Media Streams
    "pcm16": {"type": "audio/pcm", "rate": 24000},  # browser-mic dev tool
}


class RealtimeConversation:
    """One live conversation (call or dev test conversation) with the OpenAI Realtime API."""

    def __init__(self, tenant: Any, pool: Any, phone_number: str, channel: str, audio: bool, audio_format: str = "g711_ulaw"):
        self.tenant = tenant
        self.pool = pool
        self.phone_number = phone_number
        self.channel = channel
        self.audio = audio
        self.audio_format = audio_format  # "g711_ulaw" (Twilio) or "pcm16" (browser-mic dev tool)
        self.escalated = False
        self._tool_calls_this_turn = 0
        self._had_function_call_this_response = False
        # For interruptions (barge-in): on a WebSocket connection (unlike
        # WebRTC) the CLIENT itself must clean up an audio response in
        # progress as soon as the customer starts talking - otherwise the
        # tail of the old answer keeps playing over the new one, which
        # sounds like a voice that changes abruptly. We track which
        # output item is streaming audio now and since when, so on an
        # interrupt we can send the best possible conversation.item.truncate.
        self._current_output_item_id: Optional[str] = None
        self._current_output_audio_started_at: Optional[float] = None
        self._ws: Optional[Any] = None
        self.call_id: Optional[int] = None

    async def connect(self) -> None:
        self.call_id = await usage_log.log_call_start(self.pool, self.tenant.client_id, self.phone_number, self.channel)
        customer, is_new = await find_or_flag_new(self.tenant, self.pool, self.phone_number)
        # On a shared number (family, office) this can be more than one record -
        # the "best guess" (customer) must then not reveal email/notes of a
        # possibly wrong person straight away (see _customer_context_text).
        all_known = [] if is_new else await local_list_customers(self.pool, self.tenant.client_id, self.phone_number)
        instructions = agent.build_voice_instructions(self.tenant, customer, is_new, all_known=all_known)

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
                    # "prompt" biases the transcription towards plausible vocabulary
                    # for THIS business (e.g. "knipbeurt" instead of the phonetically
                    # similar "kniebeurt") - generic via tenant.niche/business_name,
                    # no hardcoded word list per niche.
                    "transcription": {
                        "model": "gpt-4o-mini-transcribe",
                        "prompt": f"Telefoongesprek met {self.tenant.business_name}, een zaak in de niche '{self.tenant.niche}'. Vlaams-Nederlands.",
                    },
                    # semantic_vad instead of server_vad: judges whether the
                    # customer's sentence feels semantically/grammatically FINISHED
                    # instead of relying only on a fixed silence timer - a thinking
                    # pause mid-sentence ("ik ben... eh... Peeters") then does not
                    # trigger a premature answer. eagerness="low" went too far
                    # (sometimes no answer at all until the customer spoke again) -
                    # "auto" is the balanced middle ground.
                    "turn_detection": {"type": "semantic_vad", "eagerness": "auto", "create_response": True},
                },
                "output": {"voice": OPENAI_REALTIME_VOICE, "format": audio_format, "speed": OPENAI_REALTIME_SPEED},
            }
        await self._ws.send(json.dumps({"type": "session.update", "session": session}))

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def start(self) -> None:
        """Lisa opens the conversation herself: no customer input needed, just
        ask for a response - she greets right away according to her instructions."""
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
        """Normalizes OpenAI Realtime events for the caller. Function calls are
        already handled internally here (agent.execute_tool) - the caller only
        sees audio/text output and status events."""
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
                # This response finished normally (not interrupted) - no
                # active output item left to cut off at the next
                # speech_started.
                self._current_output_item_id = None
                self._current_output_audio_started_at = None
                usage = (event.get("response") or {}).get("usage") or {}
                cached_input_tokens = (usage.get("input_token_details") or {}).get("cached_tokens", 0)
                try:
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
                except Exception:
                    # A DB hiccup while logging costs must never stop the
                    # conversation itself - just log and continue (see also
                    # the broader catch-all in agent.execute_tool).
                    logger.exception("usage_log.log_usage faalde voor tenant %s", self.tenant.client_id)
                # One response can contain SEVERAL function calls (parallel
                # tool calls). Only here, after response.done, send one
                # response.create to continue - not per handled tool call
                # (see _handle_function_call): otherwise every tool call starts
                # its own new response, and those audio streams overlap in the
                # playback queue - it sounds like a voice/accent switching
                # mid-sentence, because it literally is two different
                # generations spliced together.
                if self._had_function_call_this_response:
                    self._had_function_call_this_response = False
                    await self._ws.send(json.dumps({"type": "response.create"}))

            elif event_type == "error":
                logger.error("OpenAI Realtime-fout voor tenant %s: %s", self.tenant.client_id, event.get("error"))
                yield {"type": "error", "error": event.get("error")}

    async def _handle_possible_interrupt(self) -> Optional[dict[str, Any]]:
        """The customer starts talking. If an audio response was still
        streaming, that is an interruption (barge-in): on a WebSocket connection
        the client itself must send conversation.item.truncate so OpenAI's own
        conversation history does not assume the customer heard the whole
        (never played) rest of that answer. We also return an
        {"type": "interrupted"} event so the caller (Twilio bridge/browser tool)
        can empty ITS OWN playback queue - otherwise the tail of the old answer
        keeps playing over the new one."""
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
            result = await agent.execute_tool(self.tenant, self.pool, self.phone_number, name, tool_input)

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
        # No response.create here - see response.done above: that happens
        # exactly once per response, even if this response contained
        # several function calls.
        return tool_event
