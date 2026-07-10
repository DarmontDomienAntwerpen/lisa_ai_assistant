from unittest.mock import MagicMock

import pytest

from app import escalation_email


@pytest.mark.asyncio
async def test_send_escalation_email_skips_silently_without_escalation_email(tenant, monkeypatch):
    tenant.escalation_email = ""
    send_mock = MagicMock()
    monkeypatch.setattr(escalation_email.gmail_sender, "send_email", send_mock)

    await escalation_email.send_escalation_email(tenant, "+32470000001", "Jan Peeters", "klacht", "klacht")

    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_send_escalation_email_skips_silently_without_gmail_config(tenant, monkeypatch):
    monkeypatch.setattr(escalation_email.gmail_sender, "is_configured", lambda: False)
    send_mock = MagicMock()
    monkeypatch.setattr(escalation_email.gmail_sender, "send_email", send_mock)

    await escalation_email.send_escalation_email(tenant, "+32470000001", "Jan Peeters", "klacht", "klacht")

    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_send_escalation_email_sends_with_expected_content(tenant, monkeypatch):
    monkeypatch.setattr(escalation_email.gmail_sender, "is_configured", lambda: True)
    send_mock = MagicMock()
    monkeypatch.setattr(escalation_email.gmail_sender, "send_email", send_mock)

    await escalation_email.send_escalation_email(
        tenant, "+32470000001", "Jan Peeters", "klacht", "klacht over vorige afspraak"
    )

    send_mock.assert_called_once()
    to, subject, body = send_mock.call_args.args
    assert to == tenant.escalation_email
    assert "+32470000001" in body
    assert "Jan Peeters" in body
    assert "Klacht" in body
    assert "klacht over vorige afspraak" in body


@pytest.mark.asyncio
async def test_send_escalation_email_never_raises_on_send_failure(tenant, monkeypatch):
    monkeypatch.setattr(escalation_email.gmail_sender, "is_configured", lambda: True)

    def _raise(*args, **kwargs):
        raise OSError("connectie geweigerd")

    monkeypatch.setattr(escalation_email.gmail_sender, "send_email", _raise)

    # Mag geen exception laten doorsijpelen — een mailprobleem mag het
    # lopende klantgesprek nooit onderbreken.
    await escalation_email.send_escalation_email(tenant, "+32470000001", "Jan Peeters", "klacht", "klacht")
