"""E-mailnotificatie naar de zaakeigenaar bij escalatie.

Geen live call-transfer: vaak is er maar één zakennummer (dat al naar
Twilio doorschakelt), dus terugbellen zou in een lus terechtkomen. Mail
werkt met eender welk toestel van de eigenaar, geen tweede telefoonlijn
nodig — zie Tenant.escalation_email.

Verstuurd via de Gmail API (app/gmail_sender.py), niet SMTP: Railway
blokkeert uitgaande SMTP-poorten op het Trial/Hobby-plan.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app import gmail_sender

logger = logging.getLogger("lisa")

_TZ = ZoneInfo("Europe/Brussels")


# Labels zoals ze in de mail moeten staan — sleutels zijn de enum-waarden uit
# de escalate_to_human tool-definitie in app/agent.py (moet in sync blijven).
_TYPE_LABELS = {
    "klacht": "Klacht",
    "dringende situatie": "Dringende situatie",
    "vraag buiten kennis": "Vraag buiten kennis van Lisa",
    "andere": "Andere",
}


async def send_escalation_email(
    tenant: Any, caller_number: str, customer_name: str, escalation_type: str, reason: str
) -> None:
    """Verstuurt een escalatiemail. Faalt nooit hard richting de aanroeper —
    een mailprobleem mag nooit het lopende gesprek met de klant beïnvloeden,
    dus fouten worden enkel luid gelogd (server-side), niet doorgegeven."""
    if not tenant.escalation_email:
        logger.warning(
            "Escalatie voor tenant %s, maar geen escalation_email ingesteld — geen mail verstuurd.",
            tenant.client_id,
        )
        return
    if not gmail_sender.is_configured():
        logger.error("Escalatiemail niet verstuurd voor tenant %s: Gmail-verzendaccount niet geconfigureerd.", tenant.client_id)
        return

    now = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M")
    type_label = _TYPE_LABELS.get(escalation_type, "Andere")
    subject = f"Lisa: {type_label.lower()} bij {tenant.business_name} — bel {caller_number} terug"
    body = (
        "Een klant had Lisa aan de lijn en moet door een medewerker opgevolgd worden.\n\n"
        f"Zaak: {tenant.business_name}\n"
        f"Klantnummer: {caller_number}\n"
        f"Naam: {customer_name or 'nog niet gekend'}\n"
        f"Type escalatie: {type_label}\n"
        f"Waarom: {reason or 'niet gespecificeerd'}\n"
        f"Tijdstip: {now}\n\n"
        "Bel de klant zo snel mogelijk terug."
    )

    try:
        await asyncio.to_thread(gmail_sender.send_email, tenant.escalation_email, subject, body)
    except Exception:
        logger.exception("Escalatiemail versturen faalde voor tenant %s", tenant.client_id)
