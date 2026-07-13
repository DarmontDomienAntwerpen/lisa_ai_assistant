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
    """Alle gekende personen op dit nummer, meest recent eerst — een gedeeld
    nummer (gezin, kantoor) kan er meerdere hebben. Elk record bevat een
    interne "_id" (rij-id) zodat local_create_customer een bestaande rij kan
    bijwerken zonder op de (niet-deterministisch versleutelde) details te
    moeten matchen."""
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
    """Standaard/beste-gok-klant voor dit nummer (meest recent gekend) — voor
    plekken die maar één klant verwachten (bv. de "nieuw of bestaand"-check).
    Gebruik local_list_customers() als alle namen op dit nummer nodig zijn."""
    customers = await local_list_customers(pool, tenant_id, phone_number)
    if not customers:
        return None
    return {k: v for k, v in customers[0].items() if k != "_id"}


async def local_create_customer(pool: asyncpg.Pool, tenant_id: str, phone_number: str, details: dict[str, Any]) -> dict[str, Any]:
    """Voegt een nieuw persoon toe op dit nummer, of vult aan als er al een
    rij met exact dezelfde naam bestaat (voorkomt onnodige duplicaten bij een
    herhaalde intake van dezelfde persoon).

    VEILIGE MERGE, geen overschrijving: enkel velden die nog LEEG staan in
    het bestaande dossier worden ingevuld met de nieuwe waarde. Gevonden via
    security-review: een blinde overschrijving zou een toevallige naam-match
    (verkeerd verstaan, of gewoon een veelvoorkomende naam) het bestaande
    e-mailadres/notities (kunnen gezondheidsdata bevatten) laten wissen of
    vervangen door verkeerde/nieuwe info, zonder enige verificatie."""
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
