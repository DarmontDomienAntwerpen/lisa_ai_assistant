"""Dev tool: one-time OAuth connection to your personal Google Calendar.

Not part of the production code. Lisa's customers later use a service
account (see integrations/google_calendar.py), but for local testing with
your own calendar you log in yourself through your browser.

Usage:
  python onboarding/google_oauth_setup.py

Requires google_oauth_client_secret.json in the project root (OAuth Desktop
app credentials from Google Cloud Console). Writes dev_google_token.json
with the refresh_token - that file feeds dev_chat_ui.py.
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
