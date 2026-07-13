"""Abstracte interface voor agenda/klantsysteem-koppelingen.

Elke niche-/klantspecifieke integratie implementeert dezelfde interface, zodat
agent.py en main.py nooit hoeven te weten welk systeem een tenant gebruikt.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional


class IntegrationError(Exception):
    """Agenda/klantsysteem faalde. Nooit stil negeren — main.py vangt dit op
    en stuurt de klant altijd een duidelijk bericht."""


class Integration(ABC):
    @abstractmethod
    async def check_availability(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Geeft beschikbare tijdslots terug binnen [start, end]."""

    @abstractmethod
    async def book(self, customer: dict[str, Any], slot: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
        """Boekt een afspraak. Geeft een bevestiging terug (of raise IntegrationError).

        Implementaties moeten de klantnaam en telefoonnummer opslaan bij de
        afspraak (bv. in extended properties), zodat find_bookings() later
        veilig kan matchen op identiteit — niet enkel op vrije tekst.
        """

    @abstractmethod
    async def find_bookings(self, customer: dict[str, Any], start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Geeft aankomende afspraken van déze klant terug binnen [start, end].

        Elk item bevat minstens booking_id, start, end, customer_name,
        phone_number — nodig voor de identiteitscheck in agent.py voor er
        geannuleerd of verplaatst wordt.
        """

    @abstractmethod
    async def cancel_booking(self, booking: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
        """Annuleert een afspraak. agent.py heeft de juiste booking al
        eenduidig bepaald (via booking_id, met dag/uur-verduidelijking bij
        meerdere kandidaten op één nummer) vóór dit aangeroepen wordt."""

    @abstractmethod
    async def reschedule_booking(self, booking: dict[str, Any], new_slot: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
        """Verplaatst een afspraak naar new_slot. Zelfde voorwaarde als
        cancel_booking: de juiste booking is al eenduidig bepaald door agent.py."""

    @abstractmethod
    async def lookup_customer(self, phone_number: str) -> Optional[dict[str, Any]]:
        """Bestaande klant? Geeft klantgegevens terug, of None als onbekend."""

    @abstractmethod
    async def create_customer(self, details: dict[str, Any]) -> dict[str, Any]:
        """Maakt een nieuwe klant aan en geeft de klantgegevens terug."""


def get_integration(tenant: Any, pool: Any) -> Integration:
    """Factory: kiest de juiste adapter op basis van tenant.calendar_type.

    Faalt hard (ValueError) bij een onbekend type — nooit stil terugvallen op
    een verkeerde integratie.
    """
    # Lazy imports om circulaire afhankelijkheden en onnodige SDK-imports te vermijden.
    if tenant.calendar_type == "google_calendar":
        from app.integrations.google_calendar import GoogleCalendarIntegration

        return GoogleCalendarIntegration(tenant, pool)
    if tenant.calendar_type == "none":
        from app.integrations.none import NoIntegration

        return NoIntegration(tenant, pool)
    raise ValueError(f"Onbekend calendar_type: {tenant.calendar_type!r}")
