"""Env vars, model choices and the shared Postgres connection pool.

No ORM: plain asyncpg, because this is a webhook layer, not a web app.
"""
from __future__ import annotations

import os
from typing import Optional

import asyncpg
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

# --- OpenAI Realtime (runs the whole conversation: speech, decisions, tools) ---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_REALTIME_MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime")
OPENAI_REALTIME_VOICE = os.environ.get("OPENAI_REALTIME_VOICE", "marin")
# 1.0 = normal, range 0.25-1.5. NOTE: according to OpenAI community reports, a
# non-default speed can affect the accent/language consistency - listen
# carefully before this goes to production; this was hard-won this session.
OPENAI_REALTIME_SPEED = float(os.environ.get("OPENAI_REALTIME_SPEED", "1.0"))
MAX_TOOL_ITERATIONS = 6  # hard cap so a conversation never runs forever

# --- Database ---
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# --- Encryption (GDPR: conversation data and customer details encrypted) ---
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
CONVERSATION_ENCRYPTION_KEY = os.environ.get("CONVERSATION_ENCRYPTION_KEY", "")

# --- App ---
# PORT is NOT read here: Procfile/Railway pass $PORT straight to uvicorn's
# --port at shell level; this file never needs it.
CONVERSATION_HISTORY_LIMIT = int(os.environ.get("CONVERSATION_HISTORY_LIMIT", "20"))
# GDPR: explicit retention period for conversation data
CONVERSATION_RETENTION_DAYS = int(os.environ.get("CONVERSATION_RETENTION_DAYS", "180"))

# --- Dashboard (internal, HTTP Basic Auth - no multi-user login, only for
# Darmont Digital itself to follow calls/costs/escalations per tenant) ---
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

# --- Pricing (base + overage per conversation above the included number) -
# used by the dashboard to show a suggested invoice amount per tenant based
# on the actual number of conversations. Only an aid/estimate, not a real
# billing integration. ---
PRICING_BASE_EUR = float(os.environ.get("PRICING_BASE_EUR", "149"))
PRICING_INCLUDED_CALLS = int(os.environ.get("PRICING_INCLUDED_CALLS", "150"))
PRICING_OVERAGE_EUR = float(os.environ.get("PRICING_OVERAGE_EUR", "0.75"))

# --- Twilio (telephony cost per customer) - estimate based on Twilio's
# published rates (twilio.com/en-us/voice/pricing/be, checked July 2026):
# inbound to a Belgian "mobile"-type number $0.0113/min + Media Streams
# $0.0044/min = $0.0157/min combined, plus $1.25/month rent per number.
# Adjust if your actual Twilio rate differs (other number type, volume
# discount, exchange rate). ---
TWILIO_PER_MINUTE_USD = float(os.environ.get("TWILIO_PER_MINUTE_USD", "0.0157"))
TWILIO_MONTHLY_NUMBER_USD = float(os.environ.get("TWILIO_MONTHLY_NUMBER_USD", "1.25"))

# Rough, fixed USD->EUR rate - only for the dashboard's overall total (to add
# USD-billed OpenAI/Twilio cost and fixed EUR costs into one amount). No live
# exchange-rate integration needed for an estimate.
USD_TO_EUR_RATE = float(os.environ.get("USD_TO_EUR_RATE", "0.92"))

# --- Fixed, shared infrastructure cost (NOT split per customer - applies to
# the whole system, whatever the number of tenants): Railway hosting (web
# service + Postgres add-on, usually on one invoice) + any other fixed
# monthly costs. Fill in what your Railway invoice actually shows - used only
# for the dashboard's overall total (margin check), never for an individual
# customer's invoice. ---
FIXED_MONTHLY_INFRA_COST_EUR = float(os.environ.get("FIXED_MONTHLY_INFRA_COST_EUR", "5.0"))

# --- Email (escalation notifications + daily backup) - Gmail API (HTTPS),
# NOT SMTP: Railway blocks outgoing SMTP ports on Trial/Hobby. One global
# Darmont Digital sender account, shared by all tenants - see
# onboarding/gmail_sender_setup.py and app/gmail_sender.py. ---
GMAIL_SENDER_OAUTH_CREDENTIALS = os.environ.get("GMAIL_SENDER_OAUTH_CREDENTIALS", "")
GMAIL_SENDER_ADDRESS = os.environ.get("GMAIL_SENDER_ADDRESS", "")

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL ontbreekt — kan geen databaseverbinding maken")
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


_cipher: Optional[Fernet] = None


def _get_cipher() -> Fernet:
    global _cipher
    if _cipher is None:
        if not CONVERSATION_ENCRYPTION_KEY:
            raise RuntimeError("CONVERSATION_ENCRYPTION_KEY ontbreekt — kan gespreksdata niet versleutelen")
        _cipher = Fernet(CONVERSATION_ENCRYPTION_KEY.encode())
    return _cipher


def encrypt_text(plaintext: str) -> str:
    return _get_cipher().encrypt(plaintext.encode()).decode()


def decrypt_text(ciphertext: str) -> str:
    return _get_cipher().decrypt(ciphertext.encode()).decode()


def validate_required_config() -> None:
    """Fails hard and loudly at startup when an essential env var is
    missing - before a real call ever comes in. Found in code review: without
    this, DATABASE_URL/CONVERSATION_ENCRYPTION_KEY were only validated lazily,
    on FIRST real use (a DB query, an encryption) - and OPENAI_API_KEY
    nowhere. A misconfigured Railway env var only showed up when a customer
    called (a silent dead line), instead of at deploy time. Call this in
    app/main.py's lifespan, before the app accepts traffic."""
    missing = []
    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")
    if not DATABASE_URL:
        missing.append("DATABASE_URL")
    if not CONVERSATION_ENCRYPTION_KEY:
        missing.append("CONVERSATION_ENCRYPTION_KEY")
    if missing:
        raise RuntimeError(
            f"Ontbrekende verplichte env-variabele(n): {', '.join(missing)} — "
            "app start niet op. Zet deze in Railway/​.env voor je verder gaat."
        )
