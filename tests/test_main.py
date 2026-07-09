from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import main


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


def test_voice_connects_media_stream_for_known_tenant(client, monkeypatch, tenant):
    monkeypatch.setattr(main.tenants, "get_tenant_by_number", AsyncMock(return_value=tenant))
    response = client.post("/voice", data={"To": tenant.twilio_number, "From": "+32470000001"})
    assert response.status_code == 200
    assert "<Connect>" in response.text
    assert "<Stream" in response.text
    assert "wss://testserver/media-stream" in response.text  # altijd wss, ook lokaal
    assert tenant.twilio_number in response.text
    assert "+32470000001" in response.text
