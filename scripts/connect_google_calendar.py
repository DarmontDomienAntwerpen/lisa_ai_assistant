"""Klant-installatiestap: Google Agenda koppelen aan een bestaande tenant.

Draai dit tijdens de installatie bij de klant, NADAT scripts/onboard_tenant.py
de tenant al heeft aangemaakt. Opent de browser met Google's eigen inlog- en
toestemmingsscherm — de klant logt in met zijn/haar eigen Google-account en
klikt toestemming, jij (of de klant) ziet nooit een wachtwoord en er wordt
niets geïnstalleerd. Schrijft de refresh_token + calendar_id rechtstreeks in
tenants.calendar_config voor deze klant — geen aparte tabel of module, dit
vult gewoon de bestaande tenant aan (zie CLAUDE.md, datamodel "tenants").

Gebruik:
  python scripts/connect_google_calendar.py <client_id>

Vereist google_oauth_client_secret.json in de projectroot (OAuth Desktop-app
credentials uit Google Cloud Console) — dezelfde die google_oauth_setup.py
gebruikt voor lokaal dev-testen.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio  # noqa: E402

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

import tenants  # noqa: E402
from config import close_pool, get_pool  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CLIENT_SECRET_PATH = Path(__file__).resolve().parent.parent / "google_oauth_client_secret.json"


async def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Gebruik: python scripts/connect_google_calendar.py <client_id>")
    client_id = sys.argv[1]

    if not CLIENT_SECRET_PATH.exists():
        raise SystemExit(f"Ontbreekt: {CLIENT_SECRET_PATH} — zet je OAuth-client-JSON daar neer.")

    pool = await get_pool()
    tenant = await tenants.get_tenant_by_client_id(pool, client_id)
    if tenant is None:
        await close_pool()
        raise SystemExit(f"Geen tenant gevonden met client_id '{client_id}' — draai eerst scripts/onboard_tenant.py.")

    print(f"Agenda koppelen voor: {tenant.business_name} ({client_id})")
    print("Browser opent zo — laat de klant inloggen met hun eigen Google-account en toestemming geven.\n")

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    credentials = flow.run_local_server(port=0)

    calendar_id = input("Calendar ID [primary]: ").strip() or "primary"

    tenant.calendar_type = "google_calendar"
    tenant.calendar_config = {
        "oauth_credentials": {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": credentials.scopes,
        },
        "calendar_id": calendar_id,
    }
    await tenants.upsert_tenant(pool, tenant)
    await close_pool()

    print(f"\nKlaar. Agenda van {tenant.business_name} is gekoppeld (calendar_id: {calendar_id}).")
    print("Test nu met een boeking/annulering voor de klant live gaat.")


if __name__ == "__main__":
    asyncio.run(main())
