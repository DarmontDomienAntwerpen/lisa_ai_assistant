from unittest.mock import MagicMock

import pytest

from app import escalation_email


@pytest.mark.asyncio
async def test_send_escalation_email_skips_silently_without_escalation_email(tenant, monkeypatch, caplog):
    tenant.escalation_email = ""
    smtp_mock = MagicMock()
    monkeypatch.setattr(escalation_email.smtplib, "SMTP", smtp_mock)

    await escalation_email.send_escalation_email(tenant, "+32470000001", "Jan Peeters", "klacht")

    smtp_mock.assert_not_called()


@pytest.mark.asyncio
async def test_send_escalation_email_skips_silently_without_smtp_config(tenant, monkeypatch):
    monkeypatch.setattr(escalation_email, "SMTP_HOST", "")
    smtp_mock = MagicMock()
    monkeypatch.setattr(escalation_email.smtplib, "SMTP", smtp_mock)

    await escalation_email.send_escalation_email(tenant, "+32470000001", "Jan Peeters", "klacht")

    smtp_mock.assert_not_called()


@pytest.mark.asyncio
async def test_send_escalation_email_sends_with_expected_content(tenant, monkeypatch):
    monkeypatch.setattr(escalation_email, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(escalation_email, "SMTP_USERNAME", "lisa@example.com")
    monkeypatch.setattr(escalation_email, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(escalation_email, "SMTP_FROM_ADDRESS", "lisa@example.com")

    smtp_instance = MagicMock()
    smtp_instance.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_instance.__exit__ = MagicMock(return_value=False)
    smtp_cls = MagicMock(return_value=smtp_instance)
    monkeypatch.setattr(escalation_email.smtplib, "SMTP", smtp_cls)

    await escalation_email.send_escalation_email(tenant, "+32470000001", "Jan Peeters", "klacht over vorige afspraak")

    smtp_instance.starttls.assert_called_once()
    smtp_instance.login.assert_called_once_with("lisa@example.com", "secret")
    smtp_instance.send_message.assert_called_once()
    sent_message = smtp_instance.send_message.call_args.args[0]
    assert sent_message["To"] == tenant.escalation_email
    assert "+32470000001" in sent_message.get_content()
    assert "Jan Peeters" in sent_message.get_content()
    assert "klacht over vorige afspraak" in sent_message.get_content()


@pytest.mark.asyncio
async def test_send_escalation_email_never_raises_on_smtp_failure(tenant, monkeypatch):
    monkeypatch.setattr(escalation_email, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(escalation_email, "SMTP_USERNAME", "lisa@example.com")
    monkeypatch.setattr(escalation_email, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(escalation_email, "SMTP_FROM_ADDRESS", "lisa@example.com")

    def _raise(*args, **kwargs):
        raise OSError("connectie geweigerd")

    monkeypatch.setattr(escalation_email.smtplib, "SMTP", _raise)

    # Mag geen exception laten doorsijpelen — een mailprobleem mag het
    # lopende klantgesprek nooit onderbreken.
    await escalation_email.send_escalation_email(tenant, "+32470000001", "Jan Peeters", "klacht")
