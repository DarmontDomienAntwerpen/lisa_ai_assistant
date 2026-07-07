"""Dev-tool: eenmalige OAuth-koppeling met je persoonlijke Google Calendar.

Niet onderdeel van de productiecode. Klanten van Lisa gebruiken later een
service-account (zie integrations/google_calendar.py), maar voor lokaal
testen met je eigen agenda log je zelf in via je browser.

Gebruik:
  python scripts/google_oauth_setup.py

Vereist google_oauth_client_secret.json in de projectroot (OAuth Desktop-app
credentials uit Google Cloud Console). Schrijft dev_google_token.json weg
met de refresh_token — dat bestand voedt dev_chat_ui.py.
"""
from __future__ import annotations

import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CLIENT_SECRET_PATH = Path(__file__).resolve().parent.parent / "google_oauth_client_secret.json"
TOKEN_PATH = Path(__file__).resolve().parent.parent / "dev_google_token.json"


def main() -> None:
    if not CLIENT_SECRET_PATH.exists():
        raise SystemExit(f"Ontbreekt: {CLIENT_SECRET_PATH} — zet je OAuth-client-JSON daar neer.")

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    credentials = flow.run_local_server(port=0)

    token_data = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": credentials.scopes,
    }
    TOKEN_PATH.write_text(json.dumps(token_data, indent=2))
    print(f"OAuth-koppeling gelukt. Token opgeslagen in {TOKEN_PATH}")


if __name__ == "__main__":
    main()
