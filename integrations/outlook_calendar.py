"""Outlook-adapter — later. Zelfde interface als google_calendar.py.

Nog geen tenant gebruikt dit. Faalt expliciet in plaats van stil te doen
alsof er een agenda-koppeling actief is, zodat een verkeerd geconfigureerde
tenant meteen opvalt in plaats van klanten fout te informeren.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from integrations.base import Integration, IntegrationError


class OutlookCalendarIntegration(Integration):
    def __init__(self, tenant: Any, pool: Any):
        self.tenant = tenant
        self.pool = pool

    async def check_availability(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        raise IntegrationError("Outlook-koppeling is nog niet geïmplementeerd")

    async def book(self, customer: dict[str, Any], slot: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
        raise IntegrationError("Outlook-koppeling is nog niet geïmplementeerd")

    async def find_bookings(self, customer: dict[str, Any], start: datetime, end: datetime) -> list[dict[str, Any]]:
        raise IntegrationError("Outlook-koppeling is nog niet geïmplementeerd")

    async def cancel_booking(self, booking: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
        raise IntegrationError("Outlook-koppeling is nog niet geïmplementeerd")

    async def reschedule_booking(self, booking: dict[str, Any], new_slot: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
        raise IntegrationError("Outlook-koppeling is nog niet geïmplementeerd")

    async def lookup_customer(self, phone_number: str) -> Optional[dict[str, Any]]:
        raise IntegrationError("Outlook-koppeling is nog niet geïmplementeerd")

    async def create_customer(self, details: dict[str, Any]) -> dict[str, Any]:
        raise IntegrationError("Outlook-koppeling is nog niet geïmplementeerd")
