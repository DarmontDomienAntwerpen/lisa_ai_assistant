"""TwiML voor inkomende oproepen, en WhatsApp send/receive via Twilio.

Call-flow: klant belt het zaaknummer → CALL_RING_TIMEOUT_SECONDS (~3x
overgaan) geen opname op tenant.escalation_contact → Twilio's statusCallback
naar /call-status meldt "no-answer" → WhatsApp-template wordt gestuurd →
Lisa neemt het gesprek over op WhatsApp.
"""
from __future__ import annotations

import json
from typing import Any

from twilio.rest import Client
from twilio.twiml.voice_response import Dial, VoiceResponse

from config import CALL_RING_TIMEOUT_SECONDS, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_MISSED_CALL_TEMPLATE_SID

_NO_ANSWER_STATUSES = {"no-answer", "busy", "failed"}

_client: Client | None = None


def _twilio_client() -> Client:
    global _client
    if _client is None:
        _client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _client


def twiml_for_incoming_call(tenant: Any, call_status_url: str) -> str:
    response = VoiceResponse()
    dial = Dial(timeout=CALL_RING_TIMEOUT_SECONDS, action=call_status_url)
    dial.number(tenant.escalation_contact)
    response.append(dial)
    return str(response)


def twiml_for_missing_tenant() -> str:
    response = VoiceResponse()
    response.say("Sorry, dit nummer is niet actief. Probeer het later opnieuw.", language="nl-NL")
    response.hangup()
    return str(response)


async def handle_call_status(tenant: Any, call_status: str, caller_number: str) -> bool:
    """Retourneert True als de WhatsApp-fallback getriggerd is (geen opname)."""
    if call_status in _NO_ANSWER_STATUSES:
        await send_whatsapp_missed_call_template(tenant, caller_number)
        return True
    return False


async def send_whatsapp_missed_call_template(tenant: Any, to_number: str) -> None:
    import asyncio

    def _send():
        _twilio_client().messages.create(
            from_=tenant.whatsapp_number,
            to=f"whatsapp:{to_number}" if not to_number.startswith("whatsapp:") else to_number,
            content_sid=TWILIO_MISSED_CALL_TEMPLATE_SID,
            content_variables=json.dumps({"1": tenant.business_name}),
        )

    await asyncio.to_thread(_send)


async def send_whatsapp_message(tenant: Any, to_number: str, body: str) -> None:
    import asyncio

    def _send():
        _twilio_client().messages.create(
            from_=tenant.whatsapp_number,
            to=f"whatsapp:{to_number}" if not to_number.startswith("whatsapp:") else to_number,
            body=body,
        )

    await asyncio.to_thread(_send)


def parse_whatsapp_webhook(form: dict[str, Any]) -> tuple[str, str, str]:
    """Geeft (from_number, to_number, body) terug uit een Twilio WhatsApp-webhook."""
    return form.get("From", ""), form.get("To", ""), form.get("Body", "")
