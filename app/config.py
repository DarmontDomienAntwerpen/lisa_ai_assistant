"""Env vars, model-keuzes, en de gedeelde Postgres connection pool.

Geen ORM: rechtstreeks asyncpg, want dit is een webhook-laag, geen webapp.
"""
from __future__ import annotations

import os
from typing import Optional

import asyncpg
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

# --- OpenAI Realtime (voert het volledige gesprek: spraak, beslissen, tools) ---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_REALTIME_MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime")
OPENAI_REALTIME_VOICE = os.environ.get("OPENAI_REALTIME_VOICE", "marin")
# 1.0 = normaal, range 0.25-1.5. LET OP: volgens OpenAI-community-rapporten kan
# een afwijkende speed het accent/de taalconsistentie beïnvloeden — grondig
# beluisteren voor dit naar productie gaat, dit werd hard bevochten deze sessie.
OPENAI_REALTIME_SPEED = float(os.environ.get("OPENAI_REALTIME_SPEED", "1.0"))
MAX_TOOL_ITERATIONS = 6  # hard cap zodat een gesprek nooit oneindig doorloopt

# --- Database ---
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# --- Encryptie (GDPR: gespreksdata en klantgegevens versleuteld) ---
# Genereer met: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
CONVERSATION_ENCRYPTION_KEY = os.environ.get("CONVERSATION_ENCRYPTION_KEY", "")

# --- App ---
# PORT wordt NIET hier gelezen: Procfile/Railway geven $PORT rechtstreeks aan
# uvicorn's --port mee op shell-niveau, dit bestand heeft het nooit nodig.
CONVERSATION_HISTORY_LIMIT = int(os.environ.get("CONVERSATION_HISTORY_LIMIT", "20"))
# GDPR: expliciete retentietermijn voor gespreksdata
CONVERSATION_RETENTION_DAYS = int(os.environ.get("CONVERSATION_RETENTION_DAYS", "180"))

# --- Dashboard (intern, HTTP Basic Auth — geen multi-user login, enkel voor
# Darmont Digital zelf om per tenant calls/kosten/escalaties op te volgen) ---
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

# --- Prijszetting (basis + overage per gesprek boven het inbegrepen aantal) —
# gebruikt door het dashboard om per tenant een aanbevolen factuurbedrag te
# tonen op basis van het effectieve aantal gesprekken. Enkel een hulpmiddel/
# schatting, geen echte facturatie-integratie. ---
PRICING_BASE_EUR = float(os.environ.get("PRICING_BASE_EUR", "149"))
PRICING_INCLUDED_CALLS = int(os.environ.get("PRICING_INCLUDED_CALLS", "150"))
PRICING_OVERAGE_EUR = float(os.environ.get("PRICING_OVERAGE_EUR", "0.75"))

# --- Twilio (telefoniekost per klant) — schatting op basis van Twilio's
# gepubliceerde tarieven (twilio.com/en-us/voice/pricing/be, geverifieerd juli
# 2026): inbound naar een Belgisch "mobile"-type nummer $0.0113/min + Media
# Streams $0.0044/min = $0.0157/min gecombineerd, plus $1.25/maand huur per
# nummer. Pas aan als je effectieve Twilio-tarief afwijkt (ander nummertype,
# volumekorting, wisselkoers). ---
TWILIO_PER_MINUTE_USD = float(os.environ.get("TWILIO_PER_MINUTE_USD", "0.0157"))
TWILIO_MONTHLY_NUMBER_USD = float(os.environ.get("TWILIO_MONTHLY_NUMBER_USD", "1.25"))

# Ruwe, vaste omrekenkoers USD->EUR — enkel voor het dashboard-totaaloverzicht
# (om USD-gefactureerde OpenAI/Twilio-kost en EUR-vaste kosten samen te tellen
# tot één bedrag). Geen live wisselkoers-integratie nodig voor een schatting.
USD_TO_EUR_RATE = float(os.environ.get("USD_TO_EUR_RATE", "0.92"))

# --- Vaste, gedeelde infrastructuurkost (NIET per klant op te splitsen —
# geldt voor het hele systeem, ongeacht aantal tenants): Railway-hosting
# (web-service + Postgres-add-on, meestal samen op één factuur) + eventuele
# andere vaste maandkosten. Vul dit in met wat je Railway-factuur effectief
# toont — enkel gebruikt voor het dashboard-totaaloverzicht (marge-check),
# nooit voor de factuur van een individuele klant. ---
FIXED_MONTHLY_INFRA_COST_EUR = float(os.environ.get("FIXED_MONTHLY_INFRA_COST_EUR", "5.0"))

# --- E-mail (escalatienotificaties + dagelijkse backup) — Gmail API (HTTPS),
# NIET SMTP: Railway blokkeert uitgaande SMTP-poorten op Trial/Hobby. Eén
# globaal Darmont Digital-verzendaccount, gedeeld door alle tenants — zie
# onboarding/gmail_sender_setup.py en app/gmail_sender.py. ---
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
