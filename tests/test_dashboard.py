from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import dashboard.router as dashboard


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(dashboard, "DASHBOARD_USERNAME", "admin")
    monkeypatch.setattr(dashboard, "DASHBOARD_PASSWORD", "secret")
    monkeypatch.setattr(dashboard, "get_pool", AsyncMock(return_value=object()))

    test_app = FastAPI()
    test_app.include_router(dashboard.router)
    return test_app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_overview_requires_auth(client):
    response = client.get("/dashboard")
    assert response.status_code == 401


def test_overview_rejects_wrong_credentials(client):
    response = client.get("/dashboard", auth=("admin", "wrong-password"))
    assert response.status_code == 401


def test_overview_fails_closed_when_password_not_configured(client, monkeypatch):
    monkeypatch.setattr(dashboard, "DASHBOARD_PASSWORD", "")
    response = client.get("/dashboard", auth=("admin", "anything"))
    assert response.status_code == 503


def test_overview_shows_tenant_stats(client, monkeypatch, tenant):
    monkeypatch.setattr(dashboard.tenants, "list_tenants", AsyncMock(return_value=[tenant]))
    monkeypatch.setattr(
        dashboard.usage_log,
        "get_tenant_usage_summary",
        AsyncMock(return_value={
            "conversation_turns": 5,
            "total_input_tokens": 100,
            "total_output_tokens": 200,
            "total_cost_usd": 1.23,
            "escalations": 1,
            "call_count": 3,
            "last_call_at": datetime(2026, 7, 7, 10, 0, tzinfo=timezone.utc),
            "total_duration_seconds": 600,
            "total_twilio_cost_usd": 0.16,
        }),
    )

    response = client.get("/dashboard", auth=("admin", "secret"))

    assert response.status_code == 200
    assert tenant.business_name in response.text
    assert "3" in response.text  # call_count
    assert "1.23" in response.text


@pytest.mark.asyncio
async def test_tenant_detail_404s_for_unknown_client_id(client, monkeypatch):
    monkeypatch.setattr(dashboard.tenants, "list_tenants", AsyncMock(return_value=[]))
    response = client.get("/dashboard/unknown_tenant", auth=("admin", "secret"))
    assert response.status_code == 404


def test_estimated_invoice_within_included_calls_is_just_the_base_price(monkeypatch):
    monkeypatch.setattr(dashboard, "PRICING_BASE_EUR", 149.0)
    monkeypatch.setattr(dashboard, "PRICING_INCLUDED_CALLS", 150)
    monkeypatch.setattr(dashboard, "PRICING_OVERAGE_EUR", 0.75)

    assert dashboard._estimated_invoice_eur(0) == 149.0
    assert dashboard._estimated_invoice_eur(150) == 149.0


def test_estimated_invoice_adds_overage_beyond_included_calls(monkeypatch):
    monkeypatch.setattr(dashboard, "PRICING_BASE_EUR", 149.0)
    monkeypatch.setattr(dashboard, "PRICING_INCLUDED_CALLS", 150)
    monkeypatch.setattr(dashboard, "PRICING_OVERAGE_EUR", 0.75)

    # regressie-anker voor het kinesist-voorbeeld: 433 gesprekken/maand
    assert dashboard._estimated_invoice_eur(433) == pytest.approx(149.0 + 283 * 0.75)


def test_transcript_view_escapes_message_content(client, monkeypatch):
    """Regressie-vangnet: gespreksinhoud komt van de klant (via STT) en mag
    nooit ongefilterd als HTML gerenderd worden."""
    monkeypatch.setattr(
        dashboard.conversation_store,
        "get_history",
        AsyncMock(return_value=[{"role": "user", "content": "<script>alert(1)</script>"}]),
    )
    response = client.get("/dashboard/kapper_devries/+32470000001", auth=("admin", "secret"))
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
