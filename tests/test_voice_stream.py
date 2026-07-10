from unittest.mock import AsyncMock

import pytest

from app import voice_stream


@pytest.mark.asyncio
async def test_notify_escalation_prefers_name_passed_from_the_call(tenant, fake_pool, monkeypatch):
    lookup_mock = AsyncMock()
    monkeypatch.setattr(voice_stream.customer_lookup, "find_or_flag_new", lookup_mock)
    send_mock = AsyncMock()
    monkeypatch.setattr(voice_stream, "send_escalation_email", send_mock)

    await voice_stream._notify_escalation(tenant, fake_pool, "+32470000001", "Peeters", "klacht", "boze klant")

    lookup_mock.assert_not_called()
    send_mock.assert_awaited_once_with(tenant, "+32470000001", "Peeters", "klacht", "boze klant")


@pytest.mark.asyncio
async def test_notify_escalation_falls_back_to_lookup_when_no_name_given(tenant, fake_pool, monkeypatch):
    lookup_mock = AsyncMock(return_value=({"name": "Janssens"}, False))
    monkeypatch.setattr(voice_stream.customer_lookup, "find_or_flag_new", lookup_mock)
    send_mock = AsyncMock()
    monkeypatch.setattr(voice_stream, "send_escalation_email", send_mock)

    await voice_stream._notify_escalation(tenant, fake_pool, "+32470000001", "", "klacht", "boze klant")

    lookup_mock.assert_awaited_once()
    send_mock.assert_awaited_once_with(tenant, "+32470000001", "Janssens", "klacht", "boze klant")
