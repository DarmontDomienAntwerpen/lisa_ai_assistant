"""Interactief: een nieuwe klant (tenant) toevoegen aan Lisa.

Niet vanuit een webhook of dashboard-UI — dit is een operationele stap die
Darmont Digital zelf draait bij het onboarden van een nieuwe zaak, zie
CLAUDE.md "Klant onboarden". Vraagt alle tenant-velden interactief op en
roept tenants.upsert_tenant() aan.

Gebruik:
  python scripts/onboard_tenant.py

Vereist dat het Twilio-nummer al gekocht/toegewezen is in de Twilio console,
met de "A call comes in" webhook op POST https://<railway-domein>/voice.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tenants  # noqa: E402
from config import close_pool, get_pool  # noqa: E402
from tenants import Tenant  # noqa: E402


def _slugify(business_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", business_name.lower()).strip("_")
    return slug or "tenant"


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _build_calendar_config() -> tuple[str, dict]:
    print("\nAgenda-koppeling:")
    print("  1) Google Calendar — service account (aanbevolen voor echte klanten)")
    print("  2) Google Calendar — persoonlijke OAuth-token (enkel voor eigen testen)")
    print("  3) Geen koppeling (Lisa noteert enkel, mens plant handmatig in)")
    choice = _prompt("Keuze (1/2/3)", "3")

    if choice == "1":
        path = _prompt("Pad naar service-account JSON-bestand")
        service_account_info = json.loads(Path(path).expanduser().read_text())
        calendar_id = _prompt("Calendar ID (bv. 'primary' of een gedeeld agenda-adres)", "primary")
        return "google_calendar", {"service_account_info": service_account_info, "calendar_id": calendar_id}

    if choice == "2":
        path = _prompt("Pad naar OAuth-token JSON (zie scripts/google_oauth_setup.py)", "dev_google_token.json")
        oauth_credentials = json.loads(Path(path).expanduser().read_text())
        calendar_id = _prompt("Calendar ID", "primary")
        return "google_calendar", {"oauth_credentials": oauth_credentials, "calendar_id": calendar_id}

    return "none", {}


async def main() -> None:
    print("--- Nieuwe klant onboarden ---\n")

    business_name = _prompt("Bedrijfsnaam (bv. 'Kapsalon De Vries')")
    default_client_id = _slugify(business_name)
    client_id = _prompt("client_id (unieke sleutel, geen spaties)", default_client_id)
    niche = _prompt("Niche (bv. 'kapper', 'tandarts', 'garage')")
    twilio_number = _prompt("Twilio-nummer (E.164, bv. +3234000001)")
    escalation_contact = _prompt("Escalatiecontact — nummer van de zaakeigenaar zelf (E.164)")

    calendar_type, calendar_config = _build_calendar_config()

    print("\nExtra system-prompt-instructies voor deze zaak (toon, begroeting, diensten).")
    print("Bv.: \"Open het gesprek altijd met 'Welkom bij Kapsalon De Vries!'. Diensten: knipbeurt, kleuring, wassen.\"")
    system_prompt_extra = input("system_prompt_extra: ").strip()

    tenant = Tenant(
        client_id=client_id,
        business_name=business_name,
        niche=niche,
        twilio_number=twilio_number,
        calendar_type=calendar_type,
        calendar_config=calendar_config,
        system_prompt_extra=system_prompt_extra,
        escalation_contact=escalation_contact,
    )

    print(f"\n--- Controleer ---\n{tenant}\n")
    if _prompt("Opslaan? (j/n)", "j").lower() not in ("j", "ja", "y", "yes"):
        print("Geannuleerd — niets opgeslagen.")
        return

    pool = await get_pool()
    await tenants.init_schema(pool)
    await tenants.upsert_tenant(pool, tenant)
    await close_pool()

    print(f"\nKlaar. Zet in Twilio de 'A call comes in'-webhook van {twilio_number} op:")
    print("  POST https://<railway-domein>/voice")
    print("Bel daarna zelf het nummer om te testen voor de klant live gaat.")


if __name__ == "__main__":
    asyncio.run(main())
