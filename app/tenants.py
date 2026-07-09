"""Tenant-lookup op basis van het binnenkomend Twilio-nummer.

Elk aangesloten bedrijf ("zaak") is een eigen tenant. Alles wat verschilt
tussen bedrijven — toon, agenda-koppeling, escalatiecontact — leeft hier in
config, nooit als if/else in agent.py of main.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

import asyncpg

from app.config import decrypt_text, encrypt_text

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenants (
    client_id TEXT PRIMARY KEY,
    business_name TEXT NOT NULL,
    niche TEXT NOT NULL,
    twilio_number TEXT NOT NULL UNIQUE,
    calendar_type TEXT NOT NULL,
    calendar_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    system_prompt_extra TEXT NOT NULL DEFAULT '',
    escalation_contact TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS escalation_email TEXT NOT NULL DEFAULT '';
-- calendar_config bevat OAuth-credentials (refresh_token) — te gevoelig om
-- onversleuteld op te slaan, net als klantnotities elders in dit systeem.
-- Kolom wordt TEXT (versleutelde JSON is geen geldige JSONB meer). Bestaande
-- waarden worden hier niet automatisch versleuteld (kan niet in pure SQL) —
-- worden dus gereset; enige impact vandaag is dat een eventuele test-
-- agenda-koppeling opnieuw gedaan moet worden.
ALTER TABLE tenants ALTER COLUMN calendar_config DROP DEFAULT;
ALTER TABLE tenants ALTER COLUMN calendar_config TYPE TEXT USING '';
ALTER TABLE tenants ALTER COLUMN calendar_config SET DEFAULT '';
"""


@dataclass
class Tenant:
    client_id: str
    business_name: str
    niche: str
    twilio_number: str
    calendar_type: str
    calendar_config: dict[str, Any] = field(default_factory=dict)
    system_prompt_extra: str = ""
    escalation_contact: str = ""
    # E-mailadres van de zaakeigenaar — bij escalatie stuurt Lisa hier een mail
    # naartoe (bellernummer + reden). Geen live call-transfer: er is vaak maar
    # één zakennummer (dat al naar Twilio doorschakelt), dus terugbellen zou
    # in een lus terechtkomen. Mail werkt met eender welk toestel van de
    # eigenaar en vereist geen tweede telefoonlijn.
    escalation_email: str = ""

    @classmethod
    def from_record(cls, record: Any) -> "Tenant":
        raw = record["calendar_config"]
        calendar_config = json.loads(decrypt_text(raw)) if raw else {}
        return cls(
            client_id=record["client_id"],
            business_name=record["business_name"],
            niche=record["niche"],
            twilio_number=record["twilio_number"],
            calendar_type=record["calendar_type"],
            calendar_config=calendar_config or {},
            system_prompt_extra=record["system_prompt_extra"] or "",
            escalation_contact=record["escalation_contact"],
            escalation_email=record["escalation_email"] or "",
        )


async def init_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


async def get_tenant_by_number(pool: asyncpg.Pool, to_number: str) -> Optional[Tenant]:
    """Zoekt de tenant op basis van het zaaknummer dat de klant belde."""
    async with pool.acquire() as conn:
        record = await conn.fetchrow("SELECT * FROM tenants WHERE twilio_number = $1", to_number)
    if record is None:
        return None
    return Tenant.from_record(record)


async def list_tenants(pool: asyncpg.Pool) -> list[Tenant]:
    """Alle aangesloten zaken — voor het dashboard (overzicht per tenant)."""
    async with pool.acquire() as conn:
        records = await conn.fetch("SELECT * FROM tenants ORDER BY business_name")
    return [Tenant.from_record(r) for r in records]


async def get_tenant_by_client_id(pool: asyncpg.Pool, client_id: str) -> Optional[Tenant]:
    """Zoekt de tenant op client_id — bv. voor onboarding-stappen die een
    reeds aangemaakte tenant verder aanvullen (zie onboarding/onboard_webapp.py)."""
    async with pool.acquire() as conn:
        record = await conn.fetchrow("SELECT * FROM tenants WHERE client_id = $1", client_id)
    if record is None:
        return None
    return Tenant.from_record(record)


async def upsert_tenant(pool: asyncpg.Pool, tenant: Tenant) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tenants (
                client_id, business_name, niche, twilio_number,
                calendar_type, calendar_config, system_prompt_extra, escalation_contact,
                escalation_email
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (client_id) DO UPDATE SET
                business_name = EXCLUDED.business_name,
                niche = EXCLUDED.niche,
                twilio_number = EXCLUDED.twilio_number,
                calendar_type = EXCLUDED.calendar_type,
                calendar_config = EXCLUDED.calendar_config,
                system_prompt_extra = EXCLUDED.system_prompt_extra,
                escalation_contact = EXCLUDED.escalation_contact,
                escalation_email = EXCLUDED.escalation_email
            """,
            tenant.client_id,
            tenant.business_name,
            tenant.niche,
            tenant.twilio_number,
            tenant.calendar_type,
            encrypt_text(json.dumps(tenant.calendar_config)),
            tenant.system_prompt_extra,
            tenant.escalation_contact,
            tenant.escalation_email,
        )
