"""TwiML voor inkomende oproepen.

Call-flow: klant belt het zaaknummer → Twilio stuurt de audio via een Media
Stream (WebSocket) naar /media-stream, waar de audio-laag (OpenAI Realtime,
zie voice_stream.py) en de brain-laag (Claude, agent.py) het gesprek voeren.
Geen dial-naar-mens-eerst meer: Lisa neemt elke oproep meteen zelf aan.
"""
from __future__ import annotations

from typing import Any

from twilio.twiml.voice_response import Connect, Stream, VoiceResponse


def twiml_for_incoming_call(tenant: Any, caller_number: str, media_stream_url: str) -> str:
    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=media_stream_url)
    stream.parameter(name="to_number", value=tenant.twilio_number)
    stream.parameter(name="caller_number", value=caller_number)
    connect.append(stream)
    response.append(connect)
    return str(response)


def twiml_for_missing_tenant() -> str:
    response = VoiceResponse()
    response.say("Sorry, dit nummer is niet actief. Probeer het later opnieuw.", language="nl-NL")
    response.hangup()
    return str(response)
