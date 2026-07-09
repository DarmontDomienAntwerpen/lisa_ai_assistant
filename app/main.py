"""FastAPI routes: /voice (TwiML) + /media-stream (WebSocket).

Webhook-/WebSocket-laag — dun. Alle logica leeft in agent.py,
realtime_client.py, voice_stream.py, tenants.py. Geen niche-specifieke code
hier.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, WebSocket

from app import conversation_store, customer_lookup, tenants, usage_log, voice_stream
from app.config import close_pool, get_pool
from app.twilio_handler import twiml_for_incoming_call, twiml_for_missing_tenant
from dashboard.router import router as dashboard_router

logger = logging.getLogger("lisa")


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
app.include_router(dashboard_router)


@app.post("/voice")
async def voice(request: Request) -> Response:
    form = await request.form()
    to_number = str(form.get("To", ""))
    caller_number = str(form.get("From", ""))

    pool = await get_pool()
    tenant = await tenants.get_tenant_by_number(pool, to_number)
    if tenant is None:
        logger.warning("Inkomende oproep voor onbekend nummer: %s", to_number)
        return Response(content=twiml_for_missing_tenant(), media_type="application/xml")

    # Hardcoded naar wss:// (nooit ws://): Twilio Media Streams vereist TLS, en
    # achter Railway's proxy leest request.url het interne schema (vaak http)
    # ook al komt de externe call binnen over https — detectie is dus onbetrouwbaar.
    media_stream_url = str(request.url_for("media_stream")).split("://", 1)[1]
    media_stream_url = f"wss://{media_stream_url}"
    return Response(
        content=twiml_for_incoming_call(tenant, caller_number, media_stream_url),
        media_type="application/xml",
    )


@app.websocket("/media-stream", name="media_stream")
async def media_stream(websocket: WebSocket) -> None:
    await voice_stream.run_media_stream(websocket)
