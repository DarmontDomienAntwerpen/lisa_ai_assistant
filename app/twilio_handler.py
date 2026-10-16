"""TwiML for incoming calls.

Call flow: the customer calls the business number -> Twilio sends the audio
via a Media Stream (WebSocket) to /media-stream, where OpenAI Realtime runs
the whole conversation (speech, decisions, tools - see voice_stream.py/
agent.py, no separate brain model, see CLAUDE.md "Architecture of the voice
conversation"). No dial-a-human-first any more: Lisa answers every call
herself right away.
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
