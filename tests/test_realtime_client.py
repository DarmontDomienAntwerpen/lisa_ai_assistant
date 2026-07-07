import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import conversation_store
import usage_log
from config import MAX_TOOL_ITERATIONS
from realtime_client import RealtimeConversation


class FakeWS:
    """Minimalistische OpenAI Realtime-websocket-mock: voorgeprogrammeerde
    inkomende events, en een log van wat de code er zelf naartoe stuurde."""

    def __init__(self, incoming=None):
        self._incoming = list(incoming or [])
        self.sent: list[dict] = []

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)

    async def close(self) -> None:
        pass


def _fake_adapter():
    return SimpleNamespace(
        find_bookings=AsyncMock(return_value=[]),
        cancel_booking=AsyncMock(return_value={"status": "cancelled", "customer_name": "Jan"}),
        reschedule_booking=AsyncMock(return_value={"status": "rescheduled"}),
        lookup_customer=AsyncMock(return_value=None),
        book=AsyncMock(return_value={"status": "confirmed", "booking_id": "new1"}),
    )


@pytest.mark.asyncio
async def test_handle_function_call_escalates_and_sends_output(tenant, fake_pool, monkeypatch):
    monkeypatch.setattr(agent, "get_integration", lambda t, p: _fake_adapter())
    conv = RealtimeConversation(tenant, fake_pool, "+32470000001", "voice", audio=True)
    conv._ws = FakeWS()

    event = await conv._handle_function_call(
        {"call_id": "c1", "name": "escalate_to_human", "arguments": json.dumps({"reason": "klacht"})}
    )

    assert event == {"type": "escalated", "reason": "klacht"}
    assert conv.escalated is True
    # Geen response.create hier — dat gebeurt centraal op response.done (zie
    # test_events_sends_exactly_one_response_create_for_multiple_tool_calls),
    # anders overlappen meerdere tool-calls in dezelfde beurt elkaars audio.
    sent_types = [m["type"] for m in conv._ws.sent]
    assert sent_types == ["conversation.item.create"]
    assert conv._ws.sent[0]["item"]["call_id"] == "c1"
    assert conv._had_function_call_this_response is True


@pytest.mark.asyncio
async def test_handle_function_call_reports_booking_event_on_cancel(tenant, fake_pool, monkeypatch):
    adapter = _fake_adapter()
    adapter.find_bookings = AsyncMock(return_value=[{"booking_id": "abc", "customer_name": "Jan", "start": "x", "end": "y"}])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)
    monkeypatch.setattr(
        agent, "find_or_flag_new",
        AsyncMock(return_value=({"phone_number": "+32470000001", "name": "Jan"}, False)),
    )
    conv = RealtimeConversation(tenant, fake_pool, "+32470000001", "voice", audio=True)
    conv._ws = FakeWS()

    event = await conv._handle_function_call({
        "call_id": "c2",
        "name": "cancel_appointment",
        "arguments": json.dumps({"booking_id": "abc", "confirmed_customer_name": "Jan"}),
    })

    assert event == {"type": "booking_event", "status": "cancelled", "customer_name": "Jan"}
    assert conv.escalated is False


@pytest.mark.asyncio
async def test_handle_function_call_caps_at_max_tool_iterations(tenant, fake_pool, monkeypatch):
    tool_mock = AsyncMock(return_value={"busy_periods": []})
    monkeypatch.setattr(agent, "execute_tool", tool_mock)
    conv = RealtimeConversation(tenant, fake_pool, "+32470000001", "voice", audio=True)
    conv._ws = FakeWS()
    conv._tool_calls_this_turn = MAX_TOOL_ITERATIONS

    event = await conv._handle_function_call(
        {"call_id": "c3", "name": "check_availability", "arguments": "{}"}
    )

    tool_mock.assert_not_called()
    assert conv.escalated is True
    assert event["type"] == "escalated"


@pytest.mark.asyncio
async def test_events_sends_exactly_one_response_create_for_multiple_tool_calls(tenant, fake_pool, monkeypatch):
    """Regressie-test voor een echte bug: als één beurt meerdere function-calls
    bevat (bv. check_availability + book_appointment), mag er maar ÉÉN
    response.create verstuurd worden na response.done — niet één per
    tool-call. Anders start elke tool-call een eigen overlappende response,
    en hun audio interleaved in de afspeel-wachtrij (klinkt als een stem/
    accent die abrupt omslaat midden in een zin)."""
    tool_mock = AsyncMock(return_value={"busy_periods": [], "fully_free": True})
    monkeypatch.setattr(agent, "execute_tool", tool_mock)
    monkeypatch.setattr(usage_log, "log_usage", AsyncMock())

    conv = RealtimeConversation(tenant, fake_pool, "+32470000001", "voice", audio=True)
    conv._ws = FakeWS([
        json.dumps({"type": "response.output_item.done", "item": {"type": "function_call", "call_id": "c1", "name": "check_availability", "arguments": "{}"}}),
        json.dumps({"type": "response.output_item.done", "item": {"type": "function_call", "call_id": "c2", "name": "book_appointment", "arguments": "{}"}}),
        json.dumps({"type": "response.done", "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}}),
    ])

    _ = [event async for event in conv.events()]

    response_create_count = sum(1 for m in conv._ws.sent if m["type"] == "response.create")
    assert response_create_count == 1
    assert tool_mock.await_count == 2


@pytest.mark.asyncio
async def test_events_persists_user_transcript_and_logs_usage(tenant, fake_pool, monkeypatch):
    append_mock = AsyncMock()
    log_usage_mock = AsyncMock()
    monkeypatch.setattr(conversation_store, "append_message", append_mock)
    monkeypatch.setattr(usage_log, "log_usage", log_usage_mock)

    conv = RealtimeConversation(tenant, fake_pool, "+32470000001", "voice", audio=True)
    conv._ws = FakeWS([
        json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Hoi"}),
        json.dumps({
            "type": "response.done",
            "response": {"usage": {"input_tokens": 10, "output_tokens": 5, "input_token_details": {"cached_tokens": 4}}},
        }),
    ])

    events = [event async for event in conv.events()]

    assert events == [{"type": "user_transcript", "text": "Hoi"}]
    append_mock.assert_awaited_once()
    log_usage_mock.assert_awaited_once()
    assert log_usage_mock.call_args.args[-3:] == (10, 5, False)
    assert log_usage_mock.call_args.kwargs["cached_input_tokens"] == 4
