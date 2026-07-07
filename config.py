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

# --- Anthropic ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL_DEFAULT = "claude-haiku-4-5-20251001"
MODEL_ESCALATION = "claude-sonnet-5"
MAX_TOOL_ITERATIONS = 6  # hard cap zodat een gesprek nooit oneindig doorloopt

# --- Twilio ---
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
# Meta/Twilio-goedgekeurde template voor het bericht na een gemiste oproep
TWILIO_MISSED_CALL_TEMPLATE_SID = os.environ.get("TWILIO_MISSED_CALL_TEMPLATE_SID", "")
CALL_RING_TIMEOUT_SECONDS = int(os.environ.get("CALL_RING_TIMEOUT_SECONDS", "15"))  # ~3x overgaan

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
