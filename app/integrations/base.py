"""Abstract interface for calendar/customer-system integrations.

Every niche- or customer-specific integration implements the same interface,
so agent.py and main.py never need to know which system a tenant uses.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional


class IntegrationError(Exception):
    """The calendar/customer system failed. Never ignore silently - main.py
    catches this and always sends the customer a clear message."""


class Integration(ABC):
    @abstractmethod
    async def check_availability(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Returns available time slots within [start, end]."""

    @abstractmethod
    async def book(self, customer: dict[str, Any], slot: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
        """Books an appointment. Returns a confirmation (or raises IntegrationError).

        Implementations must store the customer name and phone number with the
        appointment (e.g. in extended properties), so find_bookings() can later
        match safely on identity - not just on free text.
        """

    @abstractmethod
    async def find_bookings(self, customer: dict[str, Any], start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Returns this customer's upcoming appointments within [start, end].

        Each item has at least booking_id, start, end, customer_name,
        phone_number - needed for the identity check in agent.py before anything
        is cancelled or rescheduled.
        """

    @abstractmethod
    async def cancel_booking(self, booking: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
        """Cancels an appointment. agent.py has already identified the right
        booking unambiguously (via booking_id, with day/time clarification when
        one number has several candidates) before this is called."""

    @abstractmethod
    async def reschedule_booking(self, booking: dict[str, Any], new_slot: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
        """Moves an appointment to new_slot. Same precondition as
        cancel_booking: agent.py has already identified the right booking."""

    @abstractmethod
    async def lookup_customer(self, phone_number: str) -> Optional[dict[str, Any]]:
        """Existing customer? Returns the customer details, or None if unknown."""

    @abstractmethod
    async def create_customer(self, details: dict[str, Any]) -> dict[str, Any]:
        """Creates a new customer and returns the customer details."""


def get_integration(tenant: Any, pool: Any) -> Integration:
    """Factory: picks the right adapter based on tenant.calendar_type.

    Fails hard (ValueError) on an unknown type - never silently falls back to
    the wrong integration.
    """
    # Lazy imports to avoid circular dependencies and needless SDK imports.
    if tenant.calendar_type == "google_calendar":
        from app.integrations.google_calendar import GoogleCalendarIntegration

        return GoogleCalendarIntegration(tenant, pool)
    if tenant.calendar_type == "none":
        from app.integrations.none import NoIntegration

        return NoIntegration(tenant, pool)
    raise ValueError(f"Onbekend calendar_type: {tenant.calendar_type!r}")
