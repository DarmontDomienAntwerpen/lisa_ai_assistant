"""E-mailnotificatie naar de zaakeigenaar bij escalatie.

Geen live call-transfer: vaak is er maar één zakennummer (dat al naar
Twilio doorschakelt), dus terugbellen zou in een lus terechtkomen. Mail
werkt met eender welk toestel van de eigenaar, geen tweede telefoonlijn
nodig — zie Tenant.escalation_email.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

from app.config import SMTP_FROM_ADDRESS, SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_USERNAME

logger = logging.getLogger("lisa")

_TZ = ZoneInfo("Europe/Brussels")


async def send_escalation_email(tenant: Any, caller_number: str, customer_name: str, reason: str) -> None:
    """Verstuurt een escalatiemail. Faalt nooit hard richting de aanroeper —
    een mailprobleem mag nooit het lopende gesprek met de klant beïnvloeden,
    dus fouten worden enkel luid gelogd (server-side), niet doorgegeven."""
    if not tenant.escalation_email:
        logger.warning(
            "Escalatie voor tenant %s, maar geen escalation_email ingesteld — geen mail verstuurd.",
            tenant.client_id,
        )
        return
    if not (SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and SMTP_FROM_ADDRESS):
        logger.error("Escalatiemail niet verstuurd voor tenant %s: SMTP niet geconfigureerd.", tenant.client_id)
        return

    now = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M")
    message = EmailMessage()
    message["Subject"] = f"Lisa: escalatie bij {tenant.business_name} — bel {caller_number} terug"
    message["From"] = SMTP_FROM_ADDRESS
    message["To"] = tenant.escalation_email
    message.set_content(
        "Een klant had Lisa aan de lijn en moet door een medewerker opgevolgd worden.\n\n"
        f"Zaak: {tenant.business_name}\n"
        f"Klantnaam: {customer_name or 'nog niet gekend'}\n"
        f"Klant belde vanaf: {caller_number}\n"
        f"Tijdstip: {now}\n"
        f"Reden: {reason or 'niet gespecificeerd'}\n\n"
        "Bel de klant zo snel mogelijk terug."
    )

    def _send() -> None:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(message)

    try:
        await asyncio.to_thread(_send)
    except Exception:
        logger.exception("Escalatiemail versturen faalde voor tenant %s", tenant.client_id)
