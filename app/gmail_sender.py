"""Verstuurt e-mail via de Gmail API (HTTPS) i.p.v. SMTP — Railway blokkeert
uitgaande SMTP-poorten (25/465/587/2525) op het Trial/Hobby-plan, gewoon
HTTPS-verkeer niet. Vastgesteld via een productie-`OSError: Network is
unreachable` op een escalatiemail die lokaal wel werkte.

Eén globaal Darmont Digital-verzendaccount (GMAIL_SENDER_OAUTH_CREDENTIALS),
losstaand van de agenda-OAuth per klant (die blijft per-tenant, calendar-only
scope, zie app/integrations/google_calendar.py). Nieuwe klanten hoeven dit
nooit opnieuw op te zetten — enkel hun escalation_email invullen.
Zie onboarding/gmail_sender_setup.py voor de eenmalige koppeling.
"""
from __future__ import annotations

import base64
import json
from email.message import EmailMessage
from typing import Any, Optional

from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build

from app.config import GMAIL_SENDER_ADDRESS, GMAIL_SENDER_OAUTH_CREDENTIALS

_service_cache: Optional[Any] = None


def _client() -> Any:
    global _service_cache
    if _service_cache is not None:
        return _service_cache
    if not GMAIL_SENDER_OAUTH_CREDENTIALS:
        raise RuntimeError("GMAIL_SENDER_OAUTH_CREDENTIALS ontbreekt — zie onboarding/gmail_sender_setup.py")
    credentials = OAuthCredentials(**json.loads(GMAIL_SENDER_OAUTH_CREDENTIALS))
    _service_cache = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    return _service_cache


def is_configured() -> bool:
    return bool(GMAIL_SENDER_OAUTH_CREDENTIALS and GMAIL_SENDER_ADDRESS)


def send_email(
    to: str,
    subject: str,
    body_text: str,
    attachment: Optional[tuple[bytes, str, str, str]] = None,
) -> None:
    """Synchroon (blocking Google API-call) — aanroepers draaien dit via
    asyncio.to_thread. attachment, indien gegeven: (payload_bytes, maintype,
    subtype, filename)."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = GMAIL_SENDER_ADDRESS
    message["To"] = to
    message.set_content(body_text)
    if attachment is not None:
        payload, maintype, subtype, filename = attachment
        message.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    _client().users().messages().send(userId="me", body={"raw": raw}).execute()
