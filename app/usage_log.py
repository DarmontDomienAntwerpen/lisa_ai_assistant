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

from app.config import TWILIO_PER_MINUTE_USD

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

-- Koppelt elke turn terug aan zijn call — nodig om kosten per gesprek te tonen
-- in het dashboard (i.p.v. enkel per tenant over 30 dagen). Nullable: bestaande
-- rijen van voor deze kolom horen bij geen bekende call.
ALTER TABLE usage_log ADD COLUMN IF NOT EXISTS call_id INTEGER REFERENCES calls(id);
CREATE INDEX IF NOT EXISTS idx_usage_log_call ON usage_log (call_id);

-- Nodig om de Twilio-telefoniekost per call te schatten (duur × tarief) —
-- gezet bij het opruimen van de media-stream (zie app/voice_stream.py).
-- NULL zolang de call nog loopt of als het proces crashte voor het einde.
ALTER TABLE calls ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ;
"""

# USD per 1M tokens (input vers, output) — geblend audio-tarief, zie docstring
# hierboven. Hardcoded per model-naam (niet enkel het huidige OPENAI_REALTIME_MODEL)
# zodat historische rijen correct geprijsd blijven ook als je van model wisselt.
PRICING_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "gpt-realtime": (32.0, 64.0),
    "gpt-realtime-2": (32.0, 64.0),
    "gpt-realtime-2.1": (32.0, 64.0),
    "gpt-realtime-2.1-mini": (10.0, 20.0),  # ~3.2x goedkoper dan het volle model
}
# USD per 1M CACHED input-tokens — apart, veel lager tarief dan vers.
CACHED_INPUT_PRICING_PER_MILLION_TOKENS: dict[str, float] = {
    "gpt-realtime": 0.40,
    "gpt-realtime-2": 0.40,
    "gpt-realtime-2.1": 0.40,
    "gpt-realtime-2.1-mini": 0.30,
}


async def init_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


def twilio_cost_usd(duration_seconds: Optional[float]) -> float:
    """Schatting van de telefoniekost (inbound + Media Streams gecombineerd
    tarief) voor één call — 0 zolang de duur nog niet gekend is (call loopt
    nog, of het proces crashte voor ended_at gezet werd). asyncpg geeft
    EXTRACT(EPOCH FROM ...) soms terug als Decimal i.p.v. float — expliciet
    casten, anders crasht de vermenigvuldiging met TWILIO_PER_MINUTE_USD."""
    if not duration_seconds:
        return 0.0
    return (float(duration_seconds) / 60) * TWILIO_PER_MINUTE_USD


def cost_components_usd(model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> dict[str, float]:
    """Splitst de kost op naar herkomst — vers input, cached input, output —
    zodat het dashboard per gesprek kan tonen waar de kosten vandaan komen.
    input_tokens is het TOTALE input-aantal (cached + vers) zoals OpenAI het
    rapporteert — cached_input_tokens zit er dus AL in."""
    input_price, output_price = PRICING_PER_MILLION_TOKENS.get(model, (0.0, 0.0))
    cached_price = CACHED_INPUT_PRICING_PER_MILLION_TOKENS.get(model, input_price)
    fresh_input_tokens = max(input_tokens - cached_input_tokens, 0)
    fresh_input_cost = (fresh_input_tokens / 1_000_000) * input_price
    cached_input_cost = (cached_input_tokens / 1_000_000) * cached_price
    output_cost = (output_tokens / 1_000_000) * output_price
    return {
        "fresh_input_cost_usd": fresh_input_cost,
        "cached_input_cost_usd": cached_input_cost,
        "output_cost_usd": output_cost,
        "total_cost_usd": fresh_input_cost + cached_input_cost + output_cost,
    }


def calculate_cost_usd(model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
    return cost_components_usd(model, input_tokens, output_tokens, cached_input_tokens)["total_cost_usd"]


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
    call_id: Optional[int] = None,
) -> None:
    cost_usd = calculate_cost_usd(model, input_tokens, output_tokens, cached_input_tokens)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO usage_log (tenant_id, phone_number, channel, model, input_tokens, cached_input_tokens, output_tokens, cost_usd, escalated, call_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
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
            call_id,
        )


async def log_call_start(pool: asyncpg.Pool, tenant_id: str, phone_number: str, channel: str) -> int:
    """Eén rij per opgezette RealtimeConversation-sessie — dit is de teller
    voor "aantal oproepen", los van usage_log (dat per gespreksbeurt logt).
    Geeft de nieuwe calls.id terug zodat log_usage() elke turn kan koppelen
    aan dit gesprek (dashboard: kosten per call)."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO calls (tenant_id, phone_number, channel) VALUES ($1, $2, $3) RETURNING id",
            tenant_id,
            phone_number,
            channel,
        )


async def log_call_end(pool: asyncpg.Pool, call_id: Optional[int]) -> None:
    """Zet ended_at bij het opruimen van de media-stream — voedt de Twilio-
    kostenschatting (duur × tarief) in het dashboard. Geen-op als call_id
    ontbreekt (bv. connect() zelf faalde voor er een call-rij was)."""
    if call_id is None:
        return
    async with pool.acquire() as conn:
        await conn.execute("UPDATE calls SET ended_at = now() WHERE id = $1", call_id)


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
            SELECT
                COUNT(*) AS call_count,
                MAX(started_at) AS last_call_at,
                COALESCE(SUM(EXTRACT(EPOCH FROM (ended_at - started_at))), 0) AS total_duration_seconds
            FROM calls WHERE tenant_id = $1 AND ($2::timestamptz IS NULL OR started_at >= $2)
            """,
            tenant_id,
            since,
        )
    summary = {**dict(usage_record), **dict(calls_record)}
    summary["total_twilio_cost_usd"] = twilio_cost_usd(summary["total_duration_seconds"])
    return summary


async def get_calls_with_cost_breakdown(pool: asyncpg.Pool, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Eén rij per call, met de token-som van al zijn beurten en een kosten-
    herkomst-breakdown (vers input / cached input / output) — voor het
    uitklapbare "waar komen de kosten vandaan"-overzicht per gesprek in het
    dashboard. Calls zonder usage_log-rijen (bv. meteen opgehangen) krijgen
    nul-kosten, geen crash."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                c.id AS call_id,
                c.phone_number,
                c.channel,
                c.started_at,
                c.ended_at,
                EXTRACT(EPOCH FROM (c.ended_at - c.started_at)) AS duration_seconds,
                COALESCE(u.turns, 0) AS turns,
                COALESCE(u.total_input_tokens, 0) AS total_input_tokens,
                COALESCE(u.total_cached_input_tokens, 0) AS total_cached_input_tokens,
                COALESCE(u.total_output_tokens, 0) AS total_output_tokens,
                COALESCE(u.total_cost_usd, 0) AS total_cost_usd,
                u.model,
                COALESCE(u.escalated, false) AS escalated
            FROM calls c
            LEFT JOIN (
                SELECT
                    call_id,
                    COUNT(*) AS turns,
                    SUM(input_tokens) AS total_input_tokens,
                    SUM(cached_input_tokens) AS total_cached_input_tokens,
                    SUM(output_tokens) AS total_output_tokens,
                    SUM(cost_usd) AS total_cost_usd,
                    MAX(model) AS model,
                    BOOL_OR(escalated) AS escalated
                FROM usage_log
                GROUP BY call_id
            ) u ON u.call_id = c.id
            WHERE c.tenant_id = $1
            ORDER BY c.started_at DESC
            LIMIT $2
            """,
            tenant_id,
            limit,
        )

    breakdown = []
    for row in rows:
        entry = dict(row)
        if entry["model"]:
            entry["cost_breakdown"] = cost_components_usd(
                entry["model"],
                entry["total_input_tokens"],
                entry["total_output_tokens"],
                entry["total_cached_input_tokens"],
            )
        else:
            entry["cost_breakdown"] = {
                "fresh_input_cost_usd": 0.0,
                "cached_input_cost_usd": 0.0,
                "output_cost_usd": 0.0,
                "total_cost_usd": 0.0,
            }
        entry["twilio_cost_usd"] = twilio_cost_usd(entry["duration_seconds"])
        entry["combined_total_cost_usd"] = entry["cost_breakdown"]["total_cost_usd"] + entry["twilio_cost_usd"]
        breakdown.append(entry)
    return breakdown
