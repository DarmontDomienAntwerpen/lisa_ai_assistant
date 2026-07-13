"""Eerste agenda-adapter — live voor de eerste klant.

Verwacht in tenant.calendar_config, één van beide:
{
  "service_account_info": {...},   # Google service account JSON (dict) — echte klanten
  "calendar_id": "primary" of een specifiek agenda-adres
}
of, voor lokaal testen met een persoonlijke (niet-Workspace) agenda:
{
  "oauth_credentials": {"token", "refresh_token", "token_uri",
                         "client_id", "client_secret", "scopes"},
  "calendar_id": "primary"
}
Zie onboarding/google_oauth_setup.py om oauth_credentials te genereren.

Google Calendar heeft geen klant-CRM, dus lookup/create_customer vallen terug
op de lokale customers-tabel (customer_lookup.py).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.customer_lookup import local_create_customer, local_lookup_customer
from app.integrations.base import Integration, IntegrationError

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# get_integration() bouwt per tool-call een VERSE GoogleCalendarIntegration-
# instantie, dus caching op instance-niveau alleen zou niets opleveren.
# Deze module-level cache hergebruikt de opgebouwde Google API-client over
# tool-calls/gesprekken heen (per tenant) — het opbouwen van de client kost
# meetbare tijd (credentials + discovery), en dat telt elke beurt mee in hoe
# snel Lisa kan antwoorden na een agenda-tool-call. Credentials-objecten van
# Google handelen token-refresh zelf al intern af, dus hergebruik is veilig.
_client_cache: dict[str, Any] = {}


class GoogleCalendarIntegration(Integration):
    def __init__(self, tenant: Any, pool: Any):
        self.tenant = tenant
        self.pool = pool
        config = tenant.calendar_config
        self.calendar_id = config.get("calendar_id", "primary")
        self._service_account_info = config.get("service_account_info")
        self._oauth_credentials = config.get("oauth_credentials")

    def _client(self):
        cached = _client_cache.get(self.tenant.client_id)
        if cached is not None:
            return cached

        if self._oauth_credentials:
            credentials = OAuthCredentials(**self._oauth_credentials)
        elif self._service_account_info:
            credentials = service_account.Credentials.from_service_account_info(
                self._service_account_info, scopes=SCOPES
            )
        else:
            raise IntegrationError(f"Geen Google-koppeling geconfigureerd voor tenant {self.tenant.client_id}")
        service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        _client_cache[self.tenant.client_id] = service
        return service

    async def check_availability(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        import asyncio

        def _query():
            service = self._client()
            body = {
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "items": [{"id": self.calendar_id}],
            }
            try:
                result = service.freebusy().query(body=body).execute()
            except HttpError as exc:
                raise IntegrationError(f"Google Calendar freebusy-check faalde: {exc}") from exc
            busy_periods = result["calendars"][self.calendar_id]["busy"]
            return busy_periods

        busy_periods = await asyncio.to_thread(_query)
        # Voor de eerste versie geven we de busy-periodes terug; de agent
        # redeneert hierover om vrije slots te presenteren aan de klant.
        return [{"busy_start": b["start"], "busy_end": b["end"]} for b in busy_periods]

    async def book(self, customer: dict[str, Any], slot: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
        import asyncio

        phone_number = customer.get("phone_number", "onbekend")
        # details["customer_name"] (wat de beller DEZE beurt zei, bepaald in
        # agent.py) is de bron van waarheid, niet het dossier zelf — een
        # gedeeld nummer kan meerdere mensen vertegenwoordigen.
        customer_name = details.get("customer_name") or customer.get("name", "")

        def _insert():
            service = self._client()
            event = {
                "summary": details.get("summary", f"Afspraak — {phone_number}"),
                "description": details.get("description", ""),
                "location": details.get("location", ""),
                "start": {"dateTime": slot["start"]},
                "end": {"dateTime": slot["end"]},
                # Identiteit apart van vrije tekst opslaan, zodat find_bookings()
                # betrouwbaar kan matchen i.p.v. te zoeken in de summary/description.
                "extendedProperties": {
                    "private": {"phone_number": phone_number, "customer_name": customer_name}
                },
            }
            try:
                return service.events().insert(calendarId=self.calendar_id, body=event).execute()
            except HttpError as exc:
                raise IntegrationError(f"Google Calendar boeking faalde: {exc}") from exc

        created = await asyncio.to_thread(_insert)
        return {"booking_id": created["id"], "start": slot["start"], "end": slot["end"], "status": "confirmed"}

    async def find_bookings(self, customer: dict[str, Any], start: datetime, end: datetime) -> list[dict[str, Any]]:
        import asyncio

        phone_number = customer.get("phone_number", "")

        def _list():
            service = self._client()
            try:
                result = (
                    service.events()
                    .list(
                        calendarId=self.calendar_id,
                        timeMin=start.isoformat(),
                        timeMax=end.isoformat(),
                        privateExtendedProperty=f"phone_number={phone_number}",
                        singleEvents=True,
                        orderBy="startTime",
                    )
                    .execute()
                )
            except HttpError as exc:
                raise IntegrationError(f"Google Calendar zoeken naar afspraken faalde: {exc}") from exc
            return result.get("items", [])

        events = await asyncio.to_thread(_list)
        return [
            {
                "booking_id": e["id"],
                "start": e["start"].get("dateTime", e["start"].get("date")),
                "end": e["end"].get("dateTime", e["end"].get("date")),
                "summary": e.get("summary", ""),
                "customer_name": e.get("extendedProperties", {}).get("private", {}).get("customer_name", ""),
                "phone_number": e.get("extendedProperties", {}).get("private", {}).get("phone_number", ""),
            }
            for e in events
            # Geannuleerde afspraken blijven zichtbaar in de agenda zelf (zie
            # cancel_booking) maar mogen niet als "actieve" boeking van deze
            # klant terugkomen.
            if e.get("extendedProperties", {}).get("private", {}).get("cancelled") != "true"
        ]

    async def cancel_booking(self, booking: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
        import asyncio

        def _patch():
            service = self._client()
            # Niet verwijderen: de kapper moet in de eigen agenda-app kunnen
            # zien wie er annuleerde. "transparency: transparent" geeft het
            # tijdslot wel weer vrij voor freebusy/nieuwe boekingen.
            body = {
                "summary": f"GEANNULEERD — {booking.get('summary', '')}".strip(" —"),
                "transparency": "transparent",
                # Grijs (Google Calendar colorId "8" = Graphite) zodat een
                # geannuleerde afspraak ook visueel meteen opvalt tussen de
                # actieve boekingen, niet enkel via de titel-prefix.
                "colorId": "8",
                "extendedProperties": {
                    "private": {
                        "phone_number": booking.get("phone_number", customer.get("phone_number", "")),
                        "customer_name": booking.get("customer_name", ""),
                        "cancelled": "true",
                    }
                },
            }
            try:
                return service.events().patch(calendarId=self.calendar_id, eventId=booking["booking_id"], body=body).execute()
            except HttpError as exc:
                raise IntegrationError(f"Google Calendar annuleren faalde: {exc}") from exc

        await asyncio.to_thread(_patch)
        return {"status": "cancelled", "booking_id": booking["booking_id"]}

    async def reschedule_booking(self, booking: dict[str, Any], new_slot: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
        import asyncio

        def _patch():
            service = self._client()
            body = {"start": {"dateTime": new_slot["start"]}, "end": {"dateTime": new_slot["end"]}}
            try:
                return service.events().patch(calendarId=self.calendar_id, eventId=booking["booking_id"], body=body).execute()
            except HttpError as exc:
                raise IntegrationError(f"Google Calendar verplaatsen faalde: {exc}") from exc

        updated = await asyncio.to_thread(_patch)
        return {"status": "rescheduled", "booking_id": updated["id"], "start": new_slot["start"], "end": new_slot["end"]}

    async def lookup_customer(self, phone_number: str) -> Optional[dict[str, Any]]:
        return await local_lookup_customer(self.pool, self.tenant.client_id, phone_number)

    async def create_customer(self, details: dict[str, Any]) -> dict[str, Any]:
        phone_number = details["phone_number"]
        rest = {k: v for k, v in details.items() if k != "phone_number"}
        return await local_create_customer(self.pool, self.tenant.client_id, phone_number, rest)
