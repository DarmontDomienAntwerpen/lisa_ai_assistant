"""Fallback voor tenants zonder agenda-koppeling.

Lisa boekt niets automatisch — ze noteert de wens van de klant, en een mens
plant de afspraak handmatig in. Geen stille faalstand: book() geeft een
duidelijke status "manual_follow_up_required" terug zodat agent.py de klant
hierover kan informeren en de escalatiecontact hierover berichten.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app.customer_lookup import local_create_customer, local_lookup_customer
from app.integrations.base import Integration


class NoIntegration(Integration):
    def __init__(self, tenant: Any, pool: Any):
        self.tenant = tenant
        self.pool = pool

    async def check_availability(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return []

    async def book(self, customer: dict[str, Any], slot: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "manual_follow_up_required",
            "requested_slot": slot,
            "details": details,
            "note": "Geen agenda-koppeling actief — een medewerker plant deze afspraak handmatig in.",
        }

    async def find_bookings(self, customer: dict[str, Any], start: datetime, end: datetime) -> list[dict[str, Any]]:
        # Zonder agenda-koppeling wordt niets automatisch bijgehouden — een
        # medewerker moet dit zelf opzoeken, we kunnen hier niets bevestigen.
        return []

    async def cancel_booking(self, booking: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "manual_follow_up_required",
            "note": "Geen agenda-koppeling actief — een medewerker verwerkt deze annulering handmatig.",
        }

    async def reschedule_booking(self, booking: dict[str, Any], new_slot: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "manual_follow_up_required",
            "requested_slot": new_slot,
            "note": "Geen agenda-koppeling actief — een medewerker verwerkt deze wijziging handmatig.",
        }

    async def lookup_customer(self, phone_number: str) -> Optional[dict[str, Any]]:
        return await local_lookup_customer(self.pool, self.tenant.client_id, phone_number)

    async def create_customer(self, details: dict[str, Any]) -> dict[str, Any]:
        phone_number = details["phone_number"]
        rest = {k: v for k, v in details.items() if k != "phone_number"}
        return await local_create_customer(self.pool, self.tenant.client_id, phone_number, rest)
