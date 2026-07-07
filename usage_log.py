"""Tokens, kosten, kanaal, escalaties — per tenant, vanaf dag 1.

Geen dashboard nu, wel de data ervoor. Prijzen hieronder zijn USD per 1M
tokens en moeten in sync blijven met de actuele Anthropic-pricing.
"""
from __future__ import annotations

from typing import Any

import asyncpg

from config import MODEL_DEFAULT, MODEL_ESCALATION

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS usage_log (
    id SERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    channel TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd NUMERIC(10, 6) NOT NULL,
    escalated BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_usage_log_tenant ON usage_log (tenant_id, created_at);
"""

# USD per 1M tokens (input, output)
PRICING_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    MODEL_DEFAULT: (1.0, 5.0),
    MODEL_ESCALATION: (3.0, 15.0),
}


async def init_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


def calculate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    input_price, output_price = PRICING_PER_MILLION_TOKENS.get(model, (0.0, 0.0))
    return (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price


async def log_usage(
    pool: asyncpg.Pool,
    tenant_id: str,
    phone_number: str,
    channel: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    escalated: bool = False,
) -> None:
    cost_usd = calculate_cost_usd(model, input_tokens, output_tokens)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO usage_log (tenant_id, phone_number, channel, model, input_tokens, output_tokens, cost_usd, escalated)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            tenant_id,
            phone_number,
            channel,
            model,
            input_tokens,
            output_tokens,
            cost_usd,
            escalated,
        )


async def get_tenant_usage_summary(pool: asyncpg.Pool, tenant_id: str) -> dict[str, Any]:
    async with pool.acquire() as conn:
        record = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS conversation_turns,
                COALESCE(SUM(input_tokens), 0) AS total_input_tokens,
                COALESCE(SUM(output_tokens), 0) AS total_output_tokens,
                COALESCE(SUM(cost_usd), 0) AS total_cost_usd,
                COALESCE(SUM(CASE WHEN escalated THEN 1 ELSE 0 END), 0) AS escalations
            FROM usage_log WHERE tenant_id = $1
            """,
            tenant_id,
        )
    return dict(record)
