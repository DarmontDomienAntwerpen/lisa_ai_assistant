"""Gesprekshistorie per (tenant, telefoonnummer).

GDPR: content wordt versleuteld opgeslagen (config.encrypt_text/decrypt_text)
— gesprekken kunnen gezondheidsgegevens bevatten (bv. een kinebehandeling).
Retentietermijn is expliciet (config.CONVERSATION_RETENTION_DAYS) en
purge_expired() moet periodiek gedraaid worden (bv. Railway cron) om oude
gespreksdata te verwijderen.
"""
from __future__ import annotations

from typing import Any

import asyncpg

from config import CONVERSATION_HISTORY_LIMIT, CONVERSATION_RETENTION_DAYS, decrypt_text, encrypt_text

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id SERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    channel TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversations_lookup
    ON conversations (tenant_id, phone_number, created_at);
"""


async def init_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


async def get_history(pool: asyncpg.Pool, tenant_id: str, phone_number: str, limit: int = CONVERSATION_HISTORY_LIMIT) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        records = await conn.fetch(
            """
            SELECT role, content FROM (
                SELECT role, content, created_at FROM conversations
                WHERE tenant_id = $1 AND phone_number = $2
                ORDER BY created_at DESC
                LIMIT $3
            ) recent
            ORDER BY created_at ASC
            """,
            tenant_id,
            phone_number,
            limit,
        )
    return [{"role": r["role"], "content": decrypt_text(r["content"])} for r in records]


async def append_message(pool: asyncpg.Pool, tenant_id: str, phone_number: str, channel: str, role: str, content: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO conversations (tenant_id, phone_number, channel, role, content)
            VALUES ($1, $2, $3, $4, $5)
            """,
            tenant_id,
            phone_number,
            channel,
            role,
            encrypt_text(content),
        )


async def purge_expired(pool: asyncpg.Pool, retention_days: int = CONVERSATION_RETENTION_DAYS) -> int:
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM conversations WHERE created_at < now() - ($1 || ' days')::interval",
            str(retention_days),
        )
    return int(result.split(" ")[-1])


async def _run_purge() -> None:
    """Entrypoint voor de Railway cron job: `python conversation_store.py`."""
    from config import close_pool, get_pool

    pool = await get_pool()
    deleted = await purge_expired(pool)
    print(f"[retentie] {deleted} verlopen gespreksberichten verwijderd (> {CONVERSATION_RETENTION_DAYS} dagen oud)")
    await close_pool()


if __name__ == "__main__":
    import asyncio

    asyncio.run(_run_purge())
