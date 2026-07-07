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

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenants (
    client_id TEXT PRIMARY KEY,
    business_name TEXT NOT NULL,
    niche TEXT NOT NULL,
    twilio_number TEXT NOT NULL UNIQUE,
    whatsapp_number TEXT NOT NULL UNIQUE,
    calendar_type TEXT NOT NULL,
    calendar_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    system_prompt_extra TEXT NOT NULL DEFAULT '',
    escalation_contact TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@dataclass
class Tenant:
    client_id: str
    business_name: str
    niche: str
    twilio_number: str
    whatsapp_number: str
    calendar_type: str
    calendar_config: dict[str, Any] = field(default_factory=dict)
    system_prompt_extra: str = ""
    escalation_contact: str = ""

    @classmethod
    def from_record(cls, record: Any) -> "Tenant":
        calendar_config = record["calendar_config"]
        if isinstance(calendar_config, str):
            calendar_config = json.loads(calendar_config)
        return cls(
            client_id=record["client_id"],
            business_name=record["business_name"],
            niche=record["niche"],
            twilio_number=record["twilio_number"],
            whatsapp_number=record["whatsapp_number"],
            calendar_type=record["calendar_type"],
            calendar_config=calendar_config or {},
            system_prompt_extra=record["system_prompt_extra"] or "",
            escalation_contact=record["escalation_contact"],
        )


def _normalize_number(number: str) -> str:
    """whatsapp:+3234... en +3234... moeten allebei matchen."""
    return number.replace("whatsapp:", "").strip()


async def init_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


async def get_tenant_by_number(pool: asyncpg.Pool, to_number: str) -> Optional[Tenant]:
    """Zoekt de tenant op basis van het nummer dat de klant belde/whatsappte."""
    normalized = _normalize_number(to_number)
    async with pool.acquire() as conn:
        record = await conn.fetchrow(
            """
            SELECT * FROM tenants
            WHERE twilio_number = $1 OR whatsapp_number = $1
               OR twilio_number = $2 OR whatsapp_number = $2
            """,
            to_number,
            normalized,
        )
    if record is None:
        return None
    return Tenant.from_record(record)


async def upsert_tenant(pool: asyncpg.Pool, tenant: Tenant) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tenants (
                client_id, business_name, niche, twilio_number, whatsapp_number,
                calendar_type, calendar_config, system_prompt_extra, escalation_contact
            ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
            ON CONFLICT (client_id) DO UPDATE SET
                business_name = EXCLUDED.business_name,
                niche = EXCLUDED.niche,
                twilio_number = EXCLUDED.twilio_number,
                whatsapp_number = EXCLUDED.whatsapp_number,
                calendar_type = EXCLUDED.calendar_type,
                calendar_config = EXCLUDED.calendar_config,
                system_prompt_extra = EXCLUDED.system_prompt_extra,
                escalation_contact = EXCLUDED.escalation_contact
            """,
            tenant.client_id,
            tenant.business_name,
            tenant.niche,
            tenant.twilio_number,
            tenant.whatsapp_number,
            tenant.calendar_type,
            json.dumps(tenant.calendar_config),
            tenant.system_prompt_extra,
            tenant.escalation_contact,
        )
