"""FastAPI routes: /voice (TwiML) + /media-stream (WebSocket).

Webhook/WebSocket layer - thin. All logic lives in agent.py,
realtime_client.py, voice_stream.py, tenants.py. No niche-specific code
here.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, WebSocket

from app import conversation_store, customer_lookup, tenants, usage_log, voice_stream
from app.config import close_pool, get_pool, validate_required_config
from app.twilio_handler import twiml_for_incoming_call, twiml_for_missing_tenant
from dashboard.router import router as dashboard_router

# CRITICAL FIX (found in monitoring review): without setting this
# explicitly, the "lisa" logger inherits Python's default root level
# (WARNING) - nearly every logger.info(...) call in the codebase (incl.
# "Gesprek geëscaleerd voor tenant...") silently disappeared, never visible
# in Railway's logs. Only warning/error/exception ever came through.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger("lisa")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fails hard and loudly on a missing env var - before traffic is
    # accepted, not only at the first real call (see CLAUDE.md rule 4,
    # "no silent failure").
    validate_required_config()
    pool = await get_pool()
    await tenants.init_schema(pool)
    await customer_lookup.init_schema(pool)
    await conversation_store.init_schema(pool)
    await usage_log.init_schema(pool)
    yield
    await close_pool()


app = FastAPI(title="Lisa — AI Secretary", lifespan=lifespan)
app.include_router(dashboard_router)


@app.get("/health")
async def health() -> dict:
    """For Railway's healthcheck: confirms the app has fully started
    (schemas migrated, DB pool ready) before traffic is routed to it during a
    deploy - prevents an incoming call from hitting an instance that is not
    ready yet."""
    return {"status": "ok"}


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

    # Hardcoded to wss:// (never ws://): Twilio Media Streams requires TLS, and
    # behind Railway's proxy request.url reads the internal scheme (often http)
    # even though the external call comes in over https - so detection is unreliable.
    media_stream_url = str(request.url_for("media_stream")).split("://", 1)[1]
    media_stream_url = f"wss://{media_stream_url}"
    return Response(
        content=twiml_for_incoming_call(tenant, caller_number, media_stream_url),
        media_type="application/xml",
    )


@app.websocket("/media-stream", name="media_stream")
async def media_stream(websocket: WebSocket) -> None:
    await voice_stream.run_media_stream(websocket)
