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
OPENAI_REALTIME_VOICE = os.environ.get("OPENAI_REALTIME_VOICE", "alloy")
MAX_TOOL_ITERATIONS = 6  # hard cap zodat een gesprek nooit oneindig doorloopt

# --- Database ---
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# --- Encryptie (GDPR: gespreksdata en klantgegevens versleuteld) ---
# Genereer met: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
CONVERSATION_ENCRYPTION_KEY = os.environ.get("CONVERSATION_ENCRYPTION_KEY", "")

# --- App ---
PORT = int(os.environ.get("PORT", "8000"))
CONVERSATION_HISTORY_LIMIT = int(os.environ.get("CONVERSATION_HISTORY_LIMIT", "20"))
# GDPR: expliciete retentietermijn voor gespreksdata
CONVERSATION_RETENTION_DAYS = int(os.environ.get("CONVERSATION_RETENTION_DAYS", "180"))

# --- Dashboard (intern, HTTP Basic Auth — geen multi-user login, enkel voor
# Darmont Digital zelf om per tenant calls/kosten/escalaties op te volgen) ---
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

# --- E-mail (escalatienotificaties naar de zaakeigenaar) — generieke SMTP,
# werkt met eender welke provider (Gmail, Zoho, een eigen domein, ...) ---
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM_ADDRESS = os.environ.get("SMTP_FROM_ADDRESS", "")

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
