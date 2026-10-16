from unittest.mock import AsyncMock

import pytest

from app import agent


def _fake_adapter(bookings, cancel_result=None, lookup_customer=None, book_result=None, busy_periods=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        check_availability=AsyncMock(return_value=busy_periods or []),
        find_bookings=AsyncMock(return_value=bookings),
        cancel_booking=AsyncMock(return_value=cancel_result or {"status": "cancelled"}),
        reschedule_booking=AsyncMock(return_value={"status": "rescheduled"}),
        lookup_customer=AsyncMock(return_value=lookup_customer),
        book=AsyncMock(return_value=book_result or {"status": "confirmed", "booking_id": "new1"}),
    )


def test_build_voice_instructions_includes_business_name_and_customer_context(tenant):
    instructions = agent.build_voice_instructions(tenant, None, is_new=True)
    assert tenant.business_name in instructions
    assert "NIEUWE klant" in instructions

    instructions_existing = agent.build_voice_instructions(tenant, {"name": "Jan"}, is_new=False)
    assert "BESTAANDE klant" in instructions_existing
    assert "Jan" in instructions_existing


def test_build_voice_instructions_hides_notes_and_email_when_number_is_shared(tenant):
    """Regression test (GDPR): a shared number (family) can have several
    records. The 'best guess' (most recent) must then NOT reveal its
    email/notes (may contain health data) before the caller is identified -
    otherwise person A's record leaks into a conversation with person B."""
    jan = {"name": "Jan Peeters", "email": "jan@test.be", "notes": "kniepijn na blessure"}
    marie = {"name": "Marie Peeters", "email": "", "notes": ""}

    instructions = agent.build_voice_instructions(tenant, jan, is_new=False, all_known=[jan, marie])

    assert "kniepijn" not in instructions
    assert "jan@test.be" not in instructions
    assert "Jan Peeters" in instructions  # the name itself is allowed (never said out loud, see the prompt rules)
    assert "Marie Peeters" in instructions
    assert "gedeeld nummer" in instructions.lower()


def test_build_voice_instructions_shows_full_details_when_number_has_one_customer(tenant):
    jan = {"name": "Jan Peeters", "email": "jan@test.be", "notes": "kniepijn na blessure"}
    instructions = agent.build_voice_instructions(tenant, jan, is_new=False, all_known=[jan])
    assert "jan@test.be" in instructions
    assert "kniepijn" in instructions


def test_build_voice_instructions_includes_niche_and_out_of_scope_guardrail(tenant):
    """Regression test: tenant.niche must really end up in the instructions -
    without it Lisa does not know a request falls outside her sector (e.g. a
    hair salon asked for a garage appointment) and may wrongly call
    check_availability/book_appointment for something that does not belong to
    the business."""
    instructions = agent.build_voice_instructions(tenant, None, is_new=True)
    assert tenant.niche in instructions
    assert "GEEN check_availability of book_appointment aan" in instructions
    assert "check_availability" in instructions
    assert "book_appointment aan" in instructions


@pytest.mark.asyncio
async def test_cancel_appointment_succeeds_based_on_phone_number_alone(tenant, fake_pool, monkeypatch):
    """Identity comes from the phone number the call comes in on - no name
    confirmation needed any more to cancel."""
    fake_pool.connection.fetchrow.return_value = None
    adapter = _fake_adapter([{"booking_id": "abc", "customer_name": "Jan Peeters", "start": "x", "end": "y"}])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)

    result = await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "cancel_appointment", {"booking_id": "abc"},
    )

    assert result["status"] == "cancelled"
    adapter.cancel_booking.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_appointment_unknown_booking_id_is_rejected(tenant, fake_pool, monkeypatch):
    fake_pool.connection.fetchrow.return_value = None
    adapter = _fake_adapter([])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)

    result = await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "cancel_appointment", {"booking_id": "onbestaand"},
    )

    assert "error" in result
    adapter.cancel_booking.assert_not_called()


@pytest.mark.asyncio
async def test_book_appointment_succeeds_for_existing_customer_without_name_confirmation(tenant, fake_pool, monkeypatch):
    """Identity comes from the phone number - booking for an existing
    customer needs no separate name confirmation any more."""
    adapter = _fake_adapter([], busy_periods=[])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)
    monkeypatch.setattr(
        agent, "find_or_flag_new",
        AsyncMock(return_value=({"phone_number": "+32470000001", "name": "Jan Peeters"}, False)),
    )

    result = await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "book_appointment",
        {"start": "2026-07-07T09:00:00", "end": "2026-07-07T09:30:00", "summary": "Kapbeurt"},
    )

    assert "error" not in result
    adapter.book.assert_awaited_once()


@pytest.mark.asyncio
async def test_book_appointment_uses_caller_name_over_stored_dossier_name(tenant, fake_pool, monkeypatch):
    """Regression test for a real bug (found in live testing against a real
    calendar): a shared phone number (e.g. a family) had one record in the
    name of the first caller (Jan) - when a SECOND family member (Marie)
    introduced herself and booked, the appointment still went into the
    calendar under Jan's name, because book_appointment always used the record
    name instead of what the caller had just said. caller_name must now take
    precedence."""
    adapter = _fake_adapter([], busy_periods=[])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)
    monkeypatch.setattr(
        agent, "find_or_flag_new",
        AsyncMock(return_value=({"phone_number": "+32470000001", "name": "Jan Peeters"}, False)),
    )

    await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "book_appointment",
        {
            "start": "2026-07-07T09:00:00", "end": "2026-07-07T09:30:00", "summary": "Kapbeurt",
            "caller_name": "Marie Peeters",
        },
    )

    _customer, _slot, details = adapter.book.call_args.args
    assert details["customer_name"] == "Marie Peeters"
    assert "Marie Peeters" in details["summary"]
    assert "Jan Peeters" not in details["summary"]


@pytest.mark.asyncio
async def test_book_appointment_falls_back_to_dossier_name_without_caller_name(tenant, fake_pool, monkeypatch):
    """If the caller gives no name in this conversation (not needed for a
    clear, short question), the known record name is still used."""
    adapter = _fake_adapter([], busy_periods=[])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)
    monkeypatch.setattr(
        agent, "find_or_flag_new",
        AsyncMock(return_value=({"phone_number": "+32470000001", "name": "Jan Peeters"}, False)),
    )

    await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "book_appointment",
        {"start": "2026-07-07T09:00:00", "end": "2026-07-07T09:30:00", "summary": "Kapbeurt"},
    )

    _customer, _slot, details = adapter.book.call_args.args
    assert details["customer_name"] == "Jan Peeters"


@pytest.mark.asyncio
async def test_book_appointment_passes_location_through(tenant, fake_pool, monkeypatch):
    adapter = _fake_adapter([], busy_periods=[])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)
    monkeypatch.setattr(agent, "find_or_flag_new", AsyncMock(return_value=(None, True)))

    await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "book_appointment",
        {
            "start": "2026-07-07T09:00:00", "end": "2026-07-07T09:30:00", "summary": "Onderhoud cv-ketel",
            "location": "Kerkstraat 12, 2000 Antwerpen",
        },
    )

    _customer, _slot, details = adapter.book.call_args.args
    assert details["location"] == "Kerkstraat 12, 2000 Antwerpen"


@pytest.mark.asyncio
async def test_book_appointment_refuses_when_slot_already_busy(tenant, fake_pool, monkeypatch):
    """Regression test: book_appointment must never rely on the model alone
    to call check_availability first. A customer who already put something in
    the calendar by hand must never be double-booked by Lisa."""
    adapter = _fake_adapter([], busy_periods=[{"busy_start": "2026-07-07T13:00:00+02:00", "busy_end": "2026-07-07T13:30:00+02:00"}])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)
    monkeypatch.setattr(agent, "find_or_flag_new", AsyncMock(return_value=(None, True)))

    result = await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "book_appointment",
        {"start": "2026-07-07T13:00:00", "end": "2026-07-07T13:30:00", "summary": "Kapbeurt"},
    )

    assert "error" in result
    adapter.book.assert_not_called()


@pytest.mark.asyncio
async def test_book_appointment_proceeds_when_slot_is_free(tenant, fake_pool, monkeypatch):
    adapter = _fake_adapter([], busy_periods=[])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)
    monkeypatch.setattr(agent, "find_or_flag_new", AsyncMock(return_value=(None, True)))

    result = await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "book_appointment",
        {"start": "2026-07-07T13:00:00", "end": "2026-07-07T13:30:00", "summary": "Kapbeurt"},
    )

    assert "error" not in result
    adapter.book.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_availability_converts_utc_busy_periods_to_local_time(tenant, fake_pool, monkeypatch):
    """Regression test for a real bug (found in live testing): Google's
    freebusy API always returns busy times in UTC, whatever time zone was
    requested. Without conversion to Europe/Brussels the model could read a
    fully BOOKED local slot as free (1 hour off in winter, 2 in summer) and
    tell the customer so - a local 14:00-15:00 slot requested in summer
    matches 12:00-13:00 UTC in Google's answer; without conversion those
    times did not look overlapping to the model."""
    adapter = _fake_adapter([], busy_periods=[{"busy_start": "2026-07-07T12:00:00Z", "busy_end": "2026-07-07T13:00:00Z"}])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)

    result = await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "check_availability",
        {"start": "2026-07-07T14:00:00+02:00", "end": "2026-07-07T15:00:00+02:00"},
    )

    assert result["fully_free"] is False
    busy = result["busy_periods"][0]
    assert busy["busy_start"] == "2026-07-07T14:00:00+02:00"
    assert busy["busy_end"] == "2026-07-07T15:00:00+02:00"


@pytest.mark.asyncio
async def test_create_customer_allows_second_person_on_shared_number(tenant, fake_pool, monkeypatch):
    """Regression test: local_create_customer (customer_lookup.py) is already
    safe for a shared number (new name = new row, no overwrite) - so
    execute_tool must no longer block this just because SOMETHING is already
    stored on this number. Without this fix a second family member (e.g.
    Katia on Darmont's number) could never get a record of their own."""
    adapter = _fake_adapter([])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)
    monkeypatch.setattr(
        agent, "find_or_flag_new",
        AsyncMock(return_value=({"phone_number": "+32470000001", "name": "Darmont"}, False)),
    )
    register_mock = AsyncMock(return_value={"phone_number": "+32470000001", "name": "Katia"})
    monkeypatch.setattr(agent, "register_new_customer", register_mock)

    result = await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "create_customer", {"name": "Katia"}
    )

    assert "error" not in result
    register_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_customer_succeeds_for_genuinely_new_number(tenant, fake_pool, monkeypatch):
    adapter = _fake_adapter([])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)
    monkeypatch.setattr(agent, "find_or_flag_new", AsyncMock(return_value=(None, True)))
    monkeypatch.setattr(
        agent, "register_new_customer",
        AsyncMock(return_value={"phone_number": "+32470000001", "name": "Katia"}),
    )

    result = await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "create_customer", {"name": "Katia"}
    )

    assert "error" not in result


@pytest.mark.asyncio
async def test_escalate_to_human_returns_escalation_contact(tenant, fake_pool, monkeypatch):
    adapter = _fake_adapter([])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)

    result = await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "escalate_to_human", {"escalation_type": "klacht", "reason": "klacht"}
    )

    assert result["status"] == "escalated"
    assert result["escalation_type"] == "klacht"
    assert result["escalation_contact"] == tenant.escalation_contact


@pytest.mark.asyncio
async def test_unknown_tool_returns_error(tenant, fake_pool, monkeypatch):
    adapter = _fake_adapter([])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)

    result = await agent.execute_tool(tenant, fake_pool, "+32470000001", "carrier_pigeon", {})

    assert "error" in result
