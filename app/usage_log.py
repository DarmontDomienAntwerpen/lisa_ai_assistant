"""Tokens, costs, channel, escalations - per tenant, from day 1.

No dashboard now, but the data for it. Prices below are USD per 1M tokens
and must stay in sync with current OpenAI Realtime pricing.

NOTE (deliberate simplification): OpenAI Realtime reports audio and text
tokens separately (each with its own, very different rate - audio is a
multiple of text), but here we only log the combined input/output totals
that `response.done` returns. cost_usd below is therefore an indication
based on a blended audio rate, not exact billing. Already accounted for:
automatic prompt caching (OpenAI reuses the audio history of earlier turns
in the same conversation at a much lower rate) - without it you overestimate
the cost by a lot, because a Realtime session resends the FULL audio
history so far as input on every turn.
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

# USD per 1M tokens (fresh input, output) - blended audio rate, see the
# docstring above. Hardcoded per model name (not only the current
# OPENAI_REALTIME_MODEL) so historical rows stay correctly priced after a
# model switch.
PRICING_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "gpt-realtime": (32.0, 64.0),
    "gpt-realtime-2": (32.0, 64.0),
    "gpt-realtime-2.1": (32.0, 64.0),
    "gpt-realtime-2.1-mini": (10.0, 20.0),  # ~3.2x cheaper than the full model
}
# USD per 1M CACHED input tokens - a separate rate, much lower than fresh.
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
    """Estimate of the telephony cost (inbound + Media Streams combined rate)
    for one call - 0 while the duration is not known yet (call still running,
    or the process crashed before ended_at was set). asyncpg sometimes returns
    EXTRACT(EPOCH FROM ...) as Decimal instead of float - cast explicitly, or
    the multiplication with TWILIO_PER_MINUTE_USD crashes."""
    if not duration_seconds:
        return 0.0
    return (float(duration_seconds) / 60) * TWILIO_PER_MINUTE_USD


def cost_components_usd(model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> dict[str, float]:
    """Splits the cost by origin - fresh input, cached input, output - so the
    dashboard can show per conversation where the costs come from.
    input_tokens is the TOTAL input count (cached + fresh) as OpenAI reports
    it - so cached_input_tokens is ALREADY included."""
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
    """One row per RealtimeConversation session set up - this is the counter
    for "number of calls", separate from usage_log (which logs per turn).
    Returns the new calls.id so log_usage() can link every turn to this call
    (dashboard: cost per call)."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO calls (tenant_id, phone_number, channel) VALUES ($1, $2, $3) RETURNING id",
            tenant_id,
            phone_number,
            channel,
        )


async def log_call_end(pool: asyncpg.Pool, call_id: Optional[int]) -> None:
    """Sets ended_at when the media stream is cleaned up - feeds the Twilio
    cost estimate (duration x rate) in the dashboard. No-op when call_id is
    missing (e.g. connect() itself failed before there was a call row)."""
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
                COUNT(DISTINCT CASE WHEN escalated THEN call_id END) AS escalations
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
    """One row per call, with the token sum of all its turns and a cost-origin
    breakdown (fresh input / cached input / output) - for the expandable
    "where do the costs come from" view per conversation in the dashboard.
    Calls without usage_log rows (e.g. hung up at once) get zero cost, not a
    crash."""
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
