import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import agent, conversation_store, usage_log
from app.config import MAX_TOOL_ITERATIONS
from app.realtime_client import RealtimeConversation


class FakeWS:
    """Minimal OpenAI Realtime websocket mock: preprogrammed incoming events,
    and a log of what the code itself sent to it."""

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
        {
            "call_id": "c1",
            "name": "escalate_to_human",
            "arguments": json.dumps({"escalation_type": "klacht", "reason": "klacht"}),
        }
    )

    assert event == {"type": "escalated", "escalation_type": "klacht", "reason": "klacht", "customer_name": ""}
    assert conv.escalated is True
    # No response.create here - that happens centrally on response.done (see
    # test_events_sends_exactly_one_response_create_for_multiple_tool_calls),
    # otherwise several tool calls in the same turn overlap each other's audio.
    sent_types = [m["type"] for m in conv._ws.sent]
    assert sent_types == ["conversation.item.create"]
    assert conv._ws.sent[0]["item"]["call_id"] == "c1"
    assert conv._had_function_call_this_response is True


@pytest.mark.asyncio
async def test_handle_function_call_escalates_with_customer_name(tenant, fake_pool, monkeypatch):
    """Regression test: when Lisa already knows the name and passes it (e.g. a
    new customer who escalates right away without ever booking), that name
    must flow into the escalated event - otherwise the staff member reading
    the escalation email does not know whom to call back."""
    monkeypatch.setattr(agent, "get_integration", lambda t, p: _fake_adapter())
    conv = RealtimeConversation(tenant, fake_pool, "+32470000001", "voice", audio=True)
    conv._ws = FakeWS()

    event = await conv._handle_function_call({
        "call_id": "c1",
        "name": "escalate_to_human",
        "arguments": json.dumps({"escalation_type": "klacht", "reason": "klacht", "customer_name": "Peeters"}),
    })

    assert event == {
        "type": "escalated",
        "escalation_type": "klacht",
        "reason": "klacht",
        "customer_name": "Peeters",
    }


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
        "arguments": json.dumps({"booking_id": "abc"}),
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
    """Regression test for a real bug: when one turn contains several function
    calls (e.g. check_availability + book_appointment), only ONE
    response.create may be sent after response.done - not one per tool call.
    Otherwise every tool call starts its own overlapping response, and their
    audio interleaves in the playback queue (sounds like a voice/accent
    switching abruptly mid-sentence)."""
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


@pytest.mark.asyncio
async def test_interrupt_during_audio_sends_truncate_and_yields_interrupted(tenant, fake_pool):
    """Regression test for a real bug: on a WebSocket connection (unlike
    WebRTC) the CLIENT itself must clean up the audio playback queue on an
    interruption - without this the tail of the old answer keeps playing over
    the new one, which sounds like a voice that changes abruptly. Expected:
    conversation.item.truncate is sent, and the caller gets an
    {"type": "interrupted"} event to empty its own playback queue
    (Twilio clear/browser buffer)."""
    conv = RealtimeConversation(tenant, fake_pool, "+32470000001", "voice", audio=True)
    conv._ws = FakeWS([
        json.dumps({"type": "response.output_audio.delta", "item_id": "item_1", "delta": "AAAA"}),
        json.dumps({"type": "input_audio_buffer.speech_started"}),
    ])

    events = [event async for event in conv.events()]

    assert {"type": "audio_delta", "payload": "AAAA"} in events
    assert {"type": "interrupted"} in events
    truncate_msgs = [m for m in conv._ws.sent if m["type"] == "conversation.item.truncate"]
    assert len(truncate_msgs) == 1
    assert truncate_msgs[0]["item_id"] == "item_1"
    assert truncate_msgs[0]["audio_end_ms"] >= 0


@pytest.mark.asyncio
async def test_speech_started_without_active_audio_does_not_send_truncate(tenant, fake_pool):
    """When the customer starts talking while Lisa was not streaming audio
    (the normal case - no interruption), no conversation.item.truncate may
    be sent."""
    conv = RealtimeConversation(tenant, fake_pool, "+32470000001", "voice", audio=True)
    conv._ws = FakeWS([json.dumps({"type": "input_audio_buffer.speech_started"})])

    events = [event async for event in conv.events()]

    assert events == []
    assert conv._ws.sent == []
