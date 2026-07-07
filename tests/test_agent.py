from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
from config import MODEL_DEFAULT, MODEL_ESCALATION


def test_select_model_defaults_to_haiku():
    assert agent.select_model("Kan ik morgen een afspraak maken?") == MODEL_DEFAULT


def test_select_model_escalates_on_complaint_keywords():
    assert agent.select_model("Ik heb een klacht over mijn vorige afspraak") == MODEL_ESCALATION
    assert agent.select_model("Ik wil met een mens spreken, dringend") == MODEL_ESCALATION


def test_select_model_always_escalates_for_existing_customer():
    """Trefwoorden zijn te broos (Vlaamse spreektaal mist ze vaak, bv. "ik kan
    ni komen" i.p.v. "niet komen") om een bestaande, mogelijk al geboekte
    klant op te vertrouwen. Een bestaande klant krijgt daarom altijd Sonnet,
    ongeacht wat er letterlijk staat."""
    assert agent.select_model("ik kan ni komen", is_new=False) == MODEL_ESCALATION
    assert agent.select_model("hallo", is_new=False) == MODEL_ESCALATION
    assert agent.select_model("hallo", is_new=True) == MODEL_DEFAULT


def _text_response(text: str, input_tokens=10, output_tokens=5):
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _tool_use_response(tool_name: str, tool_input: dict, tool_use_id="tool_1"):
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", id=tool_use_id, name=tool_name, input=tool_input)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


@pytest.mark.asyncio
async def test_handle_turn_returns_text_reply_and_persists_history(tenant, fake_pool, monkeypatch):
    fake_pool.connection.fetchrow.return_value = None  # nieuwe klant, geen conversation history
    fake_pool.connection.fetch.return_value = []

    mock_create = AsyncMock(return_value=_text_response("Hallo! Ben je een nieuwe klant?"))
    monkeypatch.setattr(agent._client.messages, "create", mock_create)

    reply, escalated, booking_events = await agent.handle_turn(tenant, fake_pool, "+32470000001", "whatsapp", "Hoi")

    assert reply == "Hallo! Ben je een nieuwe klant?"
    assert escalated is False
    # twee berichten gepersisteerd: het inkomende bericht van de klant + het antwoord
    insert_calls = [c for c in fake_pool.connection.execute.call_args_list if "INSERT INTO conversations" in c.args[0]]
    assert len(insert_calls) == 2


@pytest.mark.asyncio
async def test_handle_turn_executes_tool_calls_before_final_reply(tenant, fake_pool, monkeypatch):
    fake_pool.connection.fetchrow.return_value = None
    fake_pool.connection.fetch.return_value = []

    mock_create = AsyncMock(
        side_effect=[
            _tool_use_response("escalate_to_human", {"reason": "klacht"}),
            _text_response("Een medewerker neemt contact op."),
        ]
    )
    monkeypatch.setattr(agent._client.messages, "create", mock_create)

    reply, escalated, booking_events = await agent.handle_turn(tenant, fake_pool, "+32470000001", "whatsapp", "Ik heb een klacht")

    assert reply == "Een medewerker neemt contact op."
    assert escalated is True
    assert mock_create.call_count == 2


@pytest.mark.asyncio
async def test_handle_turn_never_returns_silently_on_api_failure(tenant, fake_pool, monkeypatch):
    fake_pool.connection.fetchrow.return_value = None
    fake_pool.connection.fetch.return_value = []

    import anthropic

    mock_create = AsyncMock(side_effect=anthropic.APIConnectionError(request=SimpleNamespace()))
    monkeypatch.setattr(agent._client.messages, "create", mock_create)

    reply, escalated, booking_events = await agent.handle_turn(tenant, fake_pool, "+32470000001", "whatsapp", "Hoi")

    assert reply  # nooit een lege/stille reactie
    assert escalated is True


@pytest.mark.asyncio
async def test_handle_turn_stops_after_max_tool_iterations(tenant, fake_pool, monkeypatch):
    fake_pool.connection.fetchrow.return_value = None
    fake_pool.connection.fetch.return_value = []

    mock_create = AsyncMock(return_value=_tool_use_response("check_availability", {"start": "2026-07-03T09:00:00", "end": "2026-07-03T10:00:00"}))
    monkeypatch.setattr(agent._client.messages, "create", mock_create)

    reply, escalated, booking_events = await agent.handle_turn(tenant, fake_pool, "+32470000001", "whatsapp", "Wanneer kan ik langskomen?")

    assert reply
    assert escalated is True
    assert mock_create.call_count == agent.MAX_TOOL_ITERATIONS


def _fake_adapter(bookings, cancel_result=None, lookup_customer=None, book_result=None):
    return SimpleNamespace(
        find_bookings=AsyncMock(return_value=bookings),
        cancel_booking=AsyncMock(return_value=cancel_result or {"status": "cancelled"}),
        reschedule_booking=AsyncMock(return_value={"status": "rescheduled"}),
        lookup_customer=AsyncMock(return_value=lookup_customer),
        book=AsyncMock(return_value=book_result or {"status": "confirmed", "booking_id": "new1"}),
    )


@pytest.mark.asyncio
async def test_cancel_appointment_blocks_on_name_mismatch(tenant, fake_pool, monkeypatch):
    """De kern van de veiligheidsmaatregel: een verkeerde naam mag nooit tot
    een annulering leiden, zelfs niet als het telefoonnummer wel klopt
    (bv. een gedeeld gezinsnummer)."""
    fake_pool.connection.fetchrow.return_value = None
    adapter = _fake_adapter([{"booking_id": "abc", "customer_name": "Jan Peeters", "start": "x", "end": "y"}])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)

    result = await agent._execute_tool(
        tenant, fake_pool, "+32470000001", "cancel_appointment",
        {"booking_id": "abc", "confirmed_customer_name": "Foute Naam"},
    )

    assert result["requires_human"] is True
    adapter.cancel_booking.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_appointment_succeeds_on_name_match(tenant, fake_pool, monkeypatch):
    fake_pool.connection.fetchrow.return_value = None
    adapter = _fake_adapter([{"booking_id": "abc", "customer_name": "Jan Peeters", "start": "x", "end": "y"}])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)

    result = await agent._execute_tool(
        tenant, fake_pool, "+32470000001", "cancel_appointment",
        {"booking_id": "abc", "confirmed_customer_name": "jan peeters"},  # andere hoofdlettering
    )

    assert result["status"] == "cancelled"
    adapter.cancel_booking.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_appointment_unknown_booking_id_is_rejected(tenant, fake_pool, monkeypatch):
    fake_pool.connection.fetchrow.return_value = None
    adapter = _fake_adapter([])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)

    result = await agent._execute_tool(
        tenant, fake_pool, "+32470000001", "cancel_appointment",
        {"booking_id": "onbestaand", "confirmed_customer_name": "Jan Peeters"},
    )

    assert "error" in result
    adapter.cancel_booking.assert_not_called()


@pytest.mark.asyncio
async def test_handle_turn_escalates_when_identity_check_fails(tenant, fake_pool, monkeypatch):
    """Een geweigerde annulering door naam-mismatch moet het hele gesprek als
    escalated markeren, zodat de escalation_contact verwittigd wordt."""
    fake_pool.connection.fetchrow.return_value = None
    fake_pool.connection.fetch.return_value = []
    adapter = _fake_adapter([{"booking_id": "abc", "customer_name": "Jan Peeters", "start": "x", "end": "y"}])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)

    mock_create = AsyncMock(
        side_effect=[
            _tool_use_response("cancel_appointment", {"booking_id": "abc", "confirmed_customer_name": "Foute Naam"}),
            _text_response("Dat kan ik niet bevestigen, een medewerker neemt contact op."),
        ]
    )
    monkeypatch.setattr(agent._client.messages, "create", mock_create)

    reply, escalated, booking_events = await agent.handle_turn(tenant, fake_pool, "+32470000001", "whatsapp", "Annuleer mijn afspraak")

    assert escalated is True
    adapter.cancel_booking.assert_not_called()


@pytest.mark.asyncio
async def test_book_appointment_blocks_on_name_mismatch_for_existing_customer(tenant, fake_pool, monkeypatch):
    """Zelfde bescherming als bij annuleren/verplaatsen, maar dan voor het
    boeken zelf: bij een gedeeld telefoonnummer mag Lisa nooit op naam van de
    verkeerde persoon boeken."""
    adapter = _fake_adapter([])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)
    monkeypatch.setattr(
        agent, "find_or_flag_new",
        AsyncMock(return_value=({"phone_number": "+32470000001", "name": "Jan Peeters"}, False)),
    )

    result = await agent._execute_tool(
        tenant, fake_pool, "+32470000001", "book_appointment",
        {"start": "2026-07-07T09:00:00", "end": "2026-07-07T09:30:00", "summary": "Kapbeurt", "confirmed_customer_name": "Foute Naam"},
        was_new_at_turn_start=False,
    )

    assert result["requires_human"] is True
    adapter.book.assert_not_called()


@pytest.mark.asyncio
async def test_book_appointment_skips_name_check_for_customer_created_this_turn(tenant, fake_pool, monkeypatch):
    """Regressie-test: create_customer en book_appointment gebeuren vaak in
    dezelfde beurt. Een verse lookup ziet die klant dan al als 'bestaand'
    (want net aangemaakt) — maar de naam-check moet zich baseren op de status
    bij het BEGIN van de beurt (was_new_at_turn_start), anders blokkeert Lisa
    onterecht elke nieuwe klant die in één beurt boekt."""
    adapter = _fake_adapter([])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)
    monkeypatch.setattr(
        agent, "find_or_flag_new",
        AsyncMock(return_value=({"phone_number": "+32470000001", "name": "Jef Bakkers"}, False)),
    )

    result = await agent._execute_tool(
        tenant, fake_pool, "+32470000001", "book_appointment",
        {"start": "2026-07-07T09:00:00", "end": "2026-07-07T09:30:00", "summary": "Kapbeurt"},
        was_new_at_turn_start=True,
    )

    assert "error" not in result
    adapter.book.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_turn_reports_booking_event_on_successful_cancel(tenant, fake_pool, monkeypatch):
    """De kapper moet apart genotificeerd kunnen worden bij een succesvolle
    annulering, los van escalatie — main.py leest dit uit booking_events."""
    fake_pool.connection.fetchrow.return_value = None
    fake_pool.connection.fetch.return_value = []
    adapter = _fake_adapter([{"booking_id": "abc", "customer_name": "Jan Peeters", "start": "x", "end": "y"}])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)

    mock_create = AsyncMock(
        side_effect=[
            _tool_use_response("cancel_appointment", {"booking_id": "abc", "confirmed_customer_name": "Jan Peeters"}),
            _text_response("Je afspraak is geannuleerd."),
        ]
    )
    monkeypatch.setattr(agent._client.messages, "create", mock_create)

    reply, escalated, booking_events = await agent.handle_turn(tenant, fake_pool, "+32470000001", "whatsapp", "Annuleer mijn afspraak")

    assert escalated is False
    assert len(booking_events) == 1
    assert booking_events[0]["type"] == "cancelled"
