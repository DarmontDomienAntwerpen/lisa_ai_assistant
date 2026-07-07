"""Voor klanten met een eigen CRM/booking-systeem.

Verwacht in tenant.calendar_config:
{
  "base_url": "https://klant-systeem.example.com/api",
  "api_key": "...",                # optioneel, gaat als Bearer-header mee
  "paths": {                        # optioneel, overschrijft de defaults
    "availability": "/availability",
    "book": "/bookings",
    "find_bookings": "/bookings/search",
    "cancel_booking": "/bookings/{booking_id}/cancel",
    "reschedule_booking": "/bookings/{booking_id}/reschedule",
    "lookup_customer": "/customers/lookup",
    "create_customer": "/customers"
  }
}
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import httpx

from integrations.base import Integration, IntegrationError

DEFAULT_PATHS = {
    "availability": "/availability",
    "book": "/bookings",
    "find_bookings": "/bookings/search",
    "cancel_booking": "/bookings/{booking_id}/cancel",
    "reschedule_booking": "/bookings/{booking_id}/reschedule",
    "lookup_customer": "/customers/lookup",
    "create_customer": "/customers",
}


class GenericRestApiIntegration(Integration):
    def __init__(self, tenant: Any, pool: Any):
        self.tenant = tenant
        self.pool = pool
        config = tenant.calendar_config
        self.base_url = config.get("base_url", "").rstrip("/")
        if not self.base_url:
            raise IntegrationError(f"Geen base_url geconfigureerd voor tenant {tenant.client_id}")
        self.api_key = config.get("api_key")
        self.paths = {**DEFAULT_PATHS, **config.get("paths", {})}

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _request(self, method: str, path_key: str, path_params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        path = self.paths[path_key].format(**(path_params or {}))
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.request(method, url, headers=self._headers(), **kwargs)
                response.raise_for_status()
                if response.content:
                    return response.json()
                return None
        except (httpx.HTTPError, ValueError) as exc:
            raise IntegrationError(f"Aanroep naar klantsysteem faalde ({path_key}): {exc}") from exc

    async def check_availability(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        result = await self._request(
            "GET", "availability", params={"start": start.isoformat(), "end": end.isoformat()}
        )
        return result or []

    async def book(self, customer: dict[str, Any], slot: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
        payload = {"customer": customer, "slot": slot, "details": details}
        result = await self._request("POST", "book", json=payload)
        return result or {}

    async def find_bookings(self, customer: dict[str, Any], start: datetime, end: datetime) -> list[dict[str, Any]]:
        result = await self._request(
            "GET",
            "find_bookings",
            params={"phone_number": customer.get("phone_number", ""), "start": start.isoformat(), "end": end.isoformat()},
        )
        return result or []

    async def cancel_booking(self, booking: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
        result = await self._request(
            "POST", "cancel_booking", path_params={"booking_id": booking["booking_id"]}, json={"customer": customer}
        )
        return result or {"status": "cancelled", "booking_id": booking["booking_id"]}

    async def reschedule_booking(self, booking: dict[str, Any], new_slot: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
        result = await self._request(
            "POST",
            "reschedule_booking",
            path_params={"booking_id": booking["booking_id"]},
            json={"new_slot": new_slot, "customer": customer},
        )
        return result or {"status": "rescheduled", "booking_id": booking["booking_id"], **new_slot}

    async def lookup_customer(self, phone_number: str) -> Optional[dict[str, Any]]:
        result = await self._request("GET", "lookup_customer", params={"phone_number": phone_number})
        return result or None

    async def create_customer(self, details: dict[str, Any]) -> dict[str, Any]:
        result = await self._request("POST", "create_customer", json=details)
        return result or details
