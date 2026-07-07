"""FastAPI routes: /voice, /call-status, /whatsapp-webhook.

Webhook-laag — dun. Alle logica leeft in agent.py, twilio_handler.py,
tenants.py. Geen niche-specifieke code hier.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse

import conversation_store
import customer_lookup
import tenants
import usage_log
from agent import handle_turn
from config import close_pool, get_pool
from twilio_handler import (
    handle_call_status,
    parse_whatsapp_webhook,
    send_whatsapp_message,
    twiml_for_incoming_call,
    twiml_for_missing_tenant,
)

logger = logging.getLogger("lisa")

FALLBACK_MESSAGE = (
    "Sorry, er ging iets mis aan onze kant. Een medewerker is op de hoogte "
    "gebracht en neemt zo snel mogelijk contact met je op."
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await get_pool()
    await tenants.init_schema(pool)
    await customer_lookup.init_schema(pool)
    await conversation_store.init_schema(pool)
    await usage_log.init_schema(pool)
    yield
    await close_pool()


app = FastAPI(title="Lisa — AI Secretary", lifespan=lifespan)


@app.post("/voice")
async def voice(request: Request) -> Response:
    form = await request.form()
    to_number = str(form.get("To", ""))
    pool = await get_pool()
    tenant = await tenants.get_tenant_by_number(pool, to_number)
    if tenant is None:
        logger.warning("Inkomende oproep voor onbekend nummer: %s", to_number)
        return Response(content=twiml_for_missing_tenant(), media_type="application/xml")

    call_status_url = str(request.url_for("call_status"))
    return Response(content=twiml_for_incoming_call(tenant, call_status_url), media_type="application/xml")


@app.post("/call-status", name="call_status")
async def call_status(request: Request) -> Response:
    form = await request.form()
    to_number = str(form.get("To", ""))
    caller_number = str(form.get("From", ""))
    dial_call_status = str(form.get("DialCallStatus", ""))

    pool = await get_pool()
    tenant = await tenants.get_tenant_by_number(pool, to_number)
    if tenant is None:
        logger.warning("Call-status voor onbekend nummer: %s", to_number)
        return Response(content="<Response></Response>", media_type="application/xml")

    try:
        triggered = await handle_call_status(tenant, dial_call_status, caller_number)
    except Exception:
        logger.exception("WhatsApp-fallback na gemiste oproep faalde voor tenant %s", tenant.client_id)
        triggered = False

    message = (
        "Bedankt voor je geduld, we sturen je meteen een WhatsApp-bericht."
        if triggered
        else "Bedankt voor je oproep."
    )
    response = VoiceResponse()
    response.say(message, language="nl-NL")
    response.hangup()
    return Response(content=str(response), media_type="application/xml")


@app.post("/whatsapp-webhook")
async def whatsapp_webhook(request: Request) -> Response:
    form = dict(await request.form())
    from_number, to_number, body = parse_whatsapp_webhook(form)

    pool = await get_pool()
    tenant = await tenants.get_tenant_by_number(pool, to_number)
    if tenant is None:
        logger.warning("WhatsApp-bericht voor onbekend nummer: %s", to_number)
        return Response(content="<Response></Response>", media_type="application/xml")

    caller_number = from_number.replace("whatsapp:", "")

    try:
        reply_text, escalated, booking_events = await handle_turn(tenant, pool, caller_number, "whatsapp", body)
    except Exception:
        logger.exception("Gesprek met Lisa faalde voor tenant %s / %s", tenant.client_id, caller_number)
        reply_text, escalated, booking_events = FALLBACK_MESSAGE, True, []

    if escalated and tenant.escalation_contact:
        try:
            await send_whatsapp_message(
                tenant,
                tenant.escalation_contact,
                f"Lisa heeft een escalatie gemeld voor klant {caller_number}. Bekijk het gesprek en neem over.",
            )
        except Exception:
            logger.exception("Kon escalation_contact niet verwittigen voor tenant %s", tenant.client_id)

    if tenant.escalation_contact:
        for event in booking_events:
            try:
                who = event.get("customer_name") or caller_number
                if event["type"] == "cancelled":
                    text = f"Afspraak geannuleerd door {who} ({caller_number})."
                else:
                    text = f"Afspraak verplaatst door {who} ({caller_number}) naar {event.get('start')}."
                await send_whatsapp_message(tenant, tenant.escalation_contact, text)
            except Exception:
                logger.exception("Kon escalation_contact niet verwittigen over boekingswijziging voor tenant %s", tenant.client_id)

    response = MessagingResponse()
    response.message(reply_text)
    return Response(content=str(response), media_type="application/xml")
