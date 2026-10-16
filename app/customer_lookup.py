"""New/existing check. Calls the right integration adapter.

Adapters without a CRM of their own (Google Calendar, "none") fall back on
the local `customers` table below - the only place that schema lives, so
there is no duplication between adapters.

GDPR: details is stored encrypted (config.encrypt_text/decrypt_text) -
customer notes can contain health data (e.g. a physiotherapy treatment).
Hence TEXT instead of JSONB: encrypted data is no longer valid JSON.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import asyncpg

from app.config import decrypt_text, encrypt_text
from app.integrations.base import get_integration

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS customers (
    id SERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, phone_number)
);
-- Een telefoonnummer kan door meerdere mensen gedeeld worden (gezin,
-- kantoor) — elk met hun eigen naam/dossier. De oude UNIQUE(tenant_id,
-- phone_number) liet maar één klant per nummer toe, wat een tweede beller
-- silently onder de naam van de eerste liet boeken. `name` zit in het
-- versleutelde `details`-veld (geen platte kolom), dus uniciteit per naam
-- wordt in Python afgedwongen (zie local_create_customer), niet via SQL.
ALTER TABLE customers DROP CONSTRAINT IF EXISTS customers_tenant_id_phone_number_key;
CREATE INDEX IF NOT EXISTS idx_customers_tenant_phone ON customers (tenant_id, phone_number);
"""


async def init_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


async def local_list_customers(pool: asyncpg.Pool, tenant_id: str, phone_number: str) -> list[dict[str, Any]]:
    """Every known person on this number, most recent first - a shared
    number (family, office) can have several. Each record carries an internal
    "_id" (row id) so local_create_customer can update an existing row without
    matching on the (non-deterministically encrypted) details."""
    async with pool.acquire() as conn:
        records = await conn.fetch(
            "SELECT id, phone_number, details FROM customers WHERE tenant_id = $1 AND phone_number = $2 ORDER BY created_at DESC",
            tenant_id,
            phone_number,
        )
    return [
        {"_id": r["id"], "phone_number": r["phone_number"], **json.loads(decrypt_text(r["details"]))}
        for r in records
    ]


async def local_lookup_customer(pool: asyncpg.Pool, tenant_id: str, phone_number: str) -> Optional[dict[str, Any]]:
    """Default/best-guess customer for this number (most recently known) - for
    places that expect only one customer (e.g. the "new or existing" check).
    Use local_list_customers() when all names on this number are needed."""
    customers = await local_list_customers(pool, tenant_id, phone_number)
    if not customers:
        return None
    return {k: v for k, v in customers[0].items() if k != "_id"}


async def local_create_customer(pool: asyncpg.Pool, tenant_id: str, phone_number: str, details: dict[str, Any]) -> dict[str, Any]:
    """Adds a new person on this number, or fills in a row that already has
    exactly the same name (avoids needless duplicates when the same person
    goes through intake again).

    SAFE MERGE, no overwrite: only fields still EMPTY in the existing record
    are filled with the new value. Found in security review: a blind
    overwrite would let a chance name match (misheard, or just a common name)
    erase the existing email/notes (may contain health data) or replace them
    with wrong/new info, without any verification."""
    new_name = (details.get("name") or "").strip().lower()
    existing = await local_list_customers(pool, tenant_id, phone_number)
    match = next((c for c in existing if (c.get("name") or "").strip().lower() == new_name), None)

    async with pool.acquire() as conn:
        if match is not None:
            merged = {k: v for k, v in match.items() if k not in ("_id", "phone_number")}
            for key, value in details.items():
                if value and not merged.get(key):
                    merged[key] = value
            await conn.execute(
                "UPDATE customers SET details = $1 WHERE id = $2",
                encrypt_text(json.dumps(merged)),
                match["_id"],
            )
            return {"phone_number": phone_number, **merged}
        else:
            await conn.execute(
                "INSERT INTO customers (tenant_id, phone_number, details) VALUES ($1, $2, $3)",
                tenant_id,
                phone_number,
                encrypt_text(json.dumps(details)),
            )
    return {"phone_number": phone_number, **details}


async def find_or_flag_new(tenant: Any, pool: asyncpg.Pool, phone_number: str) -> tuple[Optional[dict[str, Any]], bool]:
    """First question of every conversation: existing or new customer?

    Returns (customer details, is_new). is_new is True when the customer
    is unknown in the connected system.
    """
    adapter = get_integration(tenant, pool)
    customer = await adapter.lookup_customer(phone_number)
    return customer, customer is None


async def register_new_customer(tenant: Any, pool: asyncpg.Pool, phone_number: str, details: dict[str, Any]) -> dict[str, Any]:
    adapter = get_integration(tenant, pool)
    full_details = {"phone_number": phone_number, **details}
    return await adapter.create_customer(full_details)
