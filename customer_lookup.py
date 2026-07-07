"""Nieuw/bestaand-check. Roept de juiste integration-adapter aan.

Adapters die zelf geen CRM hebben (Google Calendar, "none") vallen terug op
de lokale `customers`-tabel hieronder — dat is de enige plek waar dat schema
leeft, zodat er geen duplicatie ontstaat tussen adapters.

GDPR: details wordt versleuteld opgeslagen (config.encrypt_text/decrypt_text)
— klantnotities kunnen gezondheidsgegevens bevatten (bv. een kinebehandeling).
Daarom TEXT in plaats van JSONB: versleutelde data is geen geldige JSON meer.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import asyncpg

from config import decrypt_text, encrypt_text
from integrations.base import get_integration

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS customers (
    id SERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, phone_number)
);
"""


async def init_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


async def local_lookup_customer(pool: asyncpg.Pool, tenant_id: str, phone_number: str) -> Optional[dict[str, Any]]:
    async with pool.acquire() as conn:
        record = await conn.fetchrow(
            "SELECT phone_number, details FROM customers WHERE tenant_id = $1 AND phone_number = $2",
            tenant_id,
            phone_number,
        )
    if record is None:
        return None
    details = json.loads(decrypt_text(record["details"]))
    return {"phone_number": record["phone_number"], **details}


async def local_create_customer(pool: asyncpg.Pool, tenant_id: str, phone_number: str, details: dict[str, Any]) -> dict[str, Any]:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO customers (tenant_id, phone_number, details)
            VALUES ($1, $2, $3)
            ON CONFLICT (tenant_id, phone_number) DO UPDATE SET details = EXCLUDED.details
            """,
            tenant_id,
            phone_number,
            encrypt_text(json.dumps(details)),
        )
    return {"phone_number": phone_number, **details}


async def find_or_flag_new(tenant: Any, pool: asyncpg.Pool, phone_number: str) -> tuple[Optional[dict[str, Any]], bool]:
    """Eerste vraag van elk gesprek: bestaande of nieuwe klant?

    Geeft (klantgegevens, is_nieuw) terug. is_nieuw is True als de klant
    onbekend is in het gekoppelde systeem.
    """
    adapter = get_integration(tenant, pool)
    customer = await adapter.lookup_customer(phone_number)
    return customer, customer is None


async def register_new_customer(tenant: Any, pool: asyncpg.Pool, phone_number: str, details: dict[str, Any]) -> dict[str, Any]:
    adapter = get_integration(tenant, pool)
    full_details = {"phone_number": phone_number, **details}
    return await adapter.create_customer(full_details)
