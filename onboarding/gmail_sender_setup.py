"""Eenmalige setup: koppel Darmont Digital's eigen Gmail-account om
escalatiemails (en dagelijkse backups) via te versturen — voor ALLE klanten
samen, niet per klant. Gebruikt de Gmail API (HTTPS) in plaats van SMTP:
Railway blokkeert uitgaande SMTP-poorten op het Trial/Hobby-plan, de Gmail
API niet (gewone HTTPS-aanroepen).

Los van de agenda-koppeling per klant (die blijft OAuth per tenant) — dit
is jouw EIGEN verzendaccount, eenmalig, geldt voor het hele systeem.

Gebruik:
  python onboarding/gmail_sender_setup.py

Print aan het einde een JSON-string — zet die als GMAIL_SENDER_OAUTH_CREDENTIALS
in Railway (railway variables --service web --set "GMAIL_SENDER_OAUTH_CREDENTIALS=<...>").
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
CLIENT_SECRET_PATH = Path(__file__).resolve().parent.parent / "google_oauth_client_secret.json"


def main() -> None:
    if not CLIENT_SECRET_PATH.exists():
        raise SystemExit(f"Ontbreekt: {CLIENT_SECRET_PATH} — zet je OAuth-client-JSON daar neer.")

    print("Browser opent zo — log in met het Gmail-account waarvan Lisa mag versturen.\n")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    credentials = flow.run_local_server(port=0)

    payload = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": credentials.scopes,
    }
    encoded = json.dumps(payload)

    print("\nKlaar. Zet dit in Railway:\n")
    print(f"  GMAIL_SENDER_OAUTH_CREDENTIALS={encoded}")
    print("\n(of: railway variables --service web --set \"GMAIL_SENDER_OAUTH_CREDENTIALS=<bovenstaande waarde>\")")


if __name__ == "__main__":
    main()
