from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(fake_pool, monkeypatch):
    monkeypatch.setattr(main, "get_pool", AsyncMock(return_value=fake_pool))
    with TestClient(main.app) as test_client:
        yield test_client


def test_voice_returns_apology_twiml_for_unknown_tenant(client, monkeypatch):
    monkeypatch.setattr(main.tenants, "get_tenant_by_number", AsyncMock(return_value=None))
    response = client.post("/voice", data={"To": "+3299999999", "From": "+32470000001"})
    assert response.status_code == 200
    assert "Sorry" in response.text


def test_voice_dials_escalation_contact_for_known_tenant(client, monkeypatch, tenant):
    monkeypatch.setattr(main.tenants, "get_tenant_by_number", AsyncMock(return_value=tenant))
    response = client.post("/voice", data={"To": tenant.twilio_number, "From": "+32470000001"})
    assert response.status_code == 200
    assert tenant.escalation_contact in response.text
    assert "<Dial" in response.text


def test_call_status_triggers_whatsapp_fallback_message_on_no_answer(client, monkeypatch, tenant):
    monkeypatch.setattr(main.tenants, "get_tenant_by_number", AsyncMock(return_value=tenant))
    monkeypatch.setattr(main, "handle_call_status", AsyncMock(return_value=True))
    response = client.post(
        "/call-status",
        data={"To": tenant.twilio_number, "From": "+32470000001", "DialCallStatus": "no-answer"},
    )
    assert response.status_code == 200
    assert "WhatsApp" in response.text


def test_whatsapp_webhook_returns_lisa_reply(client, monkeypatch, tenant):
    monkeypatch.setattr(main.tenants, "get_tenant_by_number", AsyncMock(return_value=tenant))
    monkeypatch.setattr(main, "handle_turn", AsyncMock(return_value=("Hallo, waarmee kan ik helpen?", False, [])))
    send_mock = AsyncMock()
    monkeypatch.setattr(main, "send_whatsapp_message", send_mock)

    response = client.post(
        "/whatsapp-webhook",
        data={"To": tenant.whatsapp_number, "From": "whatsapp:+32470000001", "Body": "Hoi"},
    )

    assert response.status_code == 200
    assert "Hallo, waarmee kan ik helpen?" in response.text
    send_mock.assert_not_called()


def test_whatsapp_webhook_notifies_escalation_contact_when_escalated(client, monkeypatch, tenant):
    monkeypatch.setattr(main.tenants, "get_tenant_by_number", AsyncMock(return_value=tenant))
    monkeypatch.setattr(main, "handle_turn", AsyncMock(return_value=("Een medewerker neemt contact op.", True, [])))
    send_mock = AsyncMock()
    monkeypatch.setattr(main, "send_whatsapp_message", send_mock)

    response = client.post(
        "/whatsapp-webhook",
        data={"To": tenant.whatsapp_number, "From": "whatsapp:+32470000001", "Body": "Ik heb een klacht"},
    )

    assert response.status_code == 200
    send_mock.assert_awaited_once()
    assert send_mock.call_args.args[1] == tenant.escalation_contact


def test_whatsapp_webhook_notifies_escalation_contact_on_booking_event(client, monkeypatch, tenant):
    """Ook zonder escalatie moet de kapper apart een WhatsApp krijgen bij een
    succesvolle annulering/verplaatsing, zodat die het niet enkel via Google
    Calendar's eigen notificaties hoeft te merken."""
    monkeypatch.setattr(main.tenants, "get_tenant_by_number", AsyncMock(return_value=tenant))
    monkeypatch.setattr(
        main,
        "handle_turn",
        AsyncMock(return_value=("Je afspraak is geannuleerd.", False, [{"type": "cancelled", "booking_id": "abc"}])),
    )
    send_mock = AsyncMock()
    monkeypatch.setattr(main, "send_whatsapp_message", send_mock)

    response = client.post(
        "/whatsapp-webhook",
        data={"To": tenant.whatsapp_number, "From": "whatsapp:+32470000001", "Body": "Annuleer mijn afspraak"},
    )

    assert response.status_code == 200
    send_mock.assert_awaited_once()
    assert send_mock.call_args.args[1] == tenant.escalation_contact
    assert "geannuleerd" in send_mock.call_args.args[2]


def test_whatsapp_webhook_never_fails_silently_on_agent_exception(client, monkeypatch, tenant):
    monkeypatch.setattr(main.tenants, "get_tenant_by_number", AsyncMock(return_value=tenant))
    monkeypatch.setattr(main, "handle_turn", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(main, "send_whatsapp_message", AsyncMock())

    response = client.post(
        "/whatsapp-webhook",
        data={"To": tenant.whatsapp_number, "From": "whatsapp:+32470000001", "Body": "Hoi"},
    )

    assert response.status_code == 200
    assert "medewerker" in response.text.lower()
