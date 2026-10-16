"""Sends email via the Gmail API (HTTPS) instead of SMTP - Railway blocks
outgoing SMTP ports (25/465/587/2525) on the Trial/Hobby plan, but not plain
HTTPS traffic. Found through a production `OSError: Network is unreachable`
on an escalation email that worked locally.

One global Darmont Digital sender account (GMAIL_SENDER_OAUTH_CREDENTIALS),
separate from the per-customer calendar OAuth (that stays per tenant,
calendar-only scope, see app/integrations/google_calendar.py). New customers
never need to set this up again - they only fill in their escalation_email.
See onboarding/gmail_sender_setup.py for the one-time connection.
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
    """Synchronous (blocking Google API call) - callers run this via
    asyncio.to_thread. attachment, if given: (payload_bytes, maintype,
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
