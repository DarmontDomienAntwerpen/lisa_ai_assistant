"""Tokens, kosten, kanaal, escalaties — per tenant, vanaf dag 1.

Geen dashboard nu, wel de data ervoor. Prijzen hieronder zijn USD per 1M
tokens en moeten in sync blijven met de actuele OpenAI Realtime-pricing.

LET OP (bewuste vereenvoudiging): OpenAI Realtime rapporteert audio- en
tekst-tokens apart (met elk een eigen, sterk verschillend tarief — audio is
een veelvoud van tekst), maar we loggen hier enkel de gecombineerde
input/output-totalen die `response.done` teruggeeft. cost_usd hieronder is
dus een indicatie op basis van een geblend audio-tarief, geen exacte
facturatie. Wel al verrekend: automatische prompt-caching (OpenAI hergebruikt
de audio-geschiedenis van eerdere beurten in eenzelfde gesprek tegen een veel
lager tarief) — zonder dat mee te tellen overschat je de kost fors, want een
Realtime-sessie stuurt bij elke beurt de VOLLEDIGE audio-geschiedenis tot dan
toe opnieuw mee als input.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import asyncpg

from config import OPENAI_REALTIME_MODEL

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS usage_log (
    id SERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    channel TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL,
    cost_usd NUMERIC(10, 6) NOT NULL,
    escalated BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_usage_log_tenant ON usage_log (tenant_id, created_at);
-- Bestaande productie-rijen (voor deze kolom bestond) hebben cached_input_tokens=0,
-- dus hun cost_usd blijft een (te hoge) schatting — enkel nieuwe rijen zijn exact.
ALTER TABLE usage_log ADD COLUMN IF NOT EXISTS cached_input_tokens INTEGER NOT NULL DEFAULT 0;

-- usage_log logt per Realtime-beurt (kan meerdere rijen per gesprek zijn) —
-- dat volstaat niet om "hoeveel OPROEPEN deze maand" te beantwoorden, wat
-- rechtstreeks de klantprijzen (call-volume-afhankelijk) onderbouwt. calls
-- logt daarom apart, één rij per opgezette RealtimeConversation-sessie.
CREATE TABLE IF NOT EXISTS calls (
    id SERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    channel TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_calls_tenant ON calls (tenant_id, started_at);
"""

# USD per 1M tokens (input vers, output) — geblend audio-tarief, zie docstring hierboven.
# gpt-realtime (GA): $32 input / $64 output per 1M audio-tokens.
PRICING_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    OPENAI_REALTIME_MODEL: (32.0, 64.0),
}
# USD per 1M CACHED input-tokens — apart, veel lager tarief dan vers.
# gpt-realtime (GA): $0.40 per 1M gecachete audio-input-tokens.
CACHED_INPUT_PRICING_PER_MILLION_TOKENS: dict[str, float] = {
    OPENAI_REALTIME_MODEL: 0.40,
}


async def init_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


def calculate_cost_usd(model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
    """input_tokens is het TOTALE input-aantal (cached + vers) zoals OpenAI
    het rapporteert — cached_input_tokens zit er dus AL in en wordt hier
    tegen het lagere cache-tarief herrekend in plaats van het volle tarief."""
    input_price, output_price = PRICING_PER_MILLION_TOKENS.get(model, (0.0, 0.0))
    cached_price = CACHED_INPUT_PRICING_PER_MILLION_TOKENS.get(model, input_price)
    fresh_input_tokens = max(input_tokens - cached_input_tokens, 0)
    return (
        (fresh_input_tokens / 1_000_000) * input_price
        + (cached_input_tokens / 1_000_000) * cached_price
        + (output_tokens / 1_000_000) * output_price
    )


async def log_usage(
    pool: asyncpg.Pool,
    tenant_id: str,
    phone_number: str,
    channel: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    escalated: bool = False,
    cached_input_tokens: int = 0,
) -> None:
    cost_usd = calculate_cost_usd(model, input_tokens, output_tokens, cached_input_tokens)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO usage_log (tenant_id, phone_number, channel, model, input_tokens, cached_input_tokens, output_tokens, cost_usd, escalated)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            tenant_id,
            phone_number,
            channel,
            model,
            input_tokens,
            cached_input_tokens,
            output_tokens,
            cost_usd,
            escalated,
        )


async def log_call_start(pool: asyncpg.Pool, tenant_id: str, phone_number: str, channel: str) -> None:
    """Eén rij per opgezette RealtimeConversation-sessie — dit is de teller
    voor "aantal oproepen", los van usage_log (dat per gespreksbeurt logt)."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO calls (tenant_id, phone_number, channel) VALUES ($1, $2, $3)",
            tenant_id,
            phone_number,
            channel,
        )


async def get_tenant_usage_summary(pool: asyncpg.Pool, tenant_id: str, since: Optional[datetime] = None) -> dict[str, Any]:
    async with pool.acquire() as conn:
        usage_record = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS conversation_turns,
                COALESCE(SUM(input_tokens), 0) AS total_input_tokens,
                COALESCE(SUM(output_tokens), 0) AS total_output_tokens,
                COALESCE(SUM(cost_usd), 0) AS total_cost_usd,
                COALESCE(SUM(CASE WHEN escalated THEN 1 ELSE 0 END), 0) AS escalations
            FROM usage_log WHERE tenant_id = $1 AND ($2::timestamptz IS NULL OR created_at >= $2)
            """,
            tenant_id,
            since,
        )
        calls_record = await conn.fetchrow(
            """
            SELECT COUNT(*) AS call_count, MAX(started_at) AS last_call_at
            FROM calls WHERE tenant_id = $1 AND ($2::timestamptz IS NULL OR started_at >= $2)
            """,
            tenant_id,
            since,
        )
    return {**dict(usage_record), **dict(calls_record)}
