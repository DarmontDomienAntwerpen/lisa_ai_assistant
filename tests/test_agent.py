from unittest.mock import AsyncMock

import pytest

import agent


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


def test_build_voice_instructions_includes_niche_and_out_of_scope_guardrail(tenant):
    """Regressie-test: tenant.niche moet echt in de instructies terechtkomen —
    zonder dit weet Lisa niet dat een verzoek buiten haar sector valt (bv. een
    kapsalon die om een garage-afspraak gevraagd wordt) en kan ze onterecht
    check_availability/book_appointment aanroepen voor iets wat niet bij de
    zaak hoort."""
    instructions = agent.build_voice_instructions(tenant, None, is_new=True)
    assert tenant.niche in instructions
    assert "GEEN check_availability of book_appointment aan" in instructions
    assert "check_availability" in instructions
    assert "book_appointment aan" in instructions


@pytest.mark.asyncio
async def test_cancel_appointment_blocks_on_name_mismatch(tenant, fake_pool, monkeypatch):
    """De kern van de veiligheidsmaatregel: een verkeerde naam mag nooit tot
    een annulering leiden, zelfs niet als het telefoonnummer wel klopt
    (bv. een gedeeld gezinsnummer)."""
    fake_pool.connection.fetchrow.return_value = None
    adapter = _fake_adapter([{"booking_id": "abc", "customer_name": "Jan Peeters", "start": "x", "end": "y"}])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)

    result = await agent.execute_tool(
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

    result = await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "cancel_appointment",
        {"booking_id": "abc", "confirmed_customer_name": "jan peeters"},  # andere hoofdlettering
    )

    assert result["status"] == "cancelled"
    adapter.cancel_booking.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_appointment_succeeds_when_name_given_as_spelled_out_letters(tenant, fake_pool, monkeypatch):
    """Regressie-test voor een echte bug (gevonden via live testen): het model
    geeft confirmed_customer_name soms door als de losse, met streepjes
    gespelde letters (bv. "P-E-E-T-E-R-S") in plaats van het samengevoegde
    woord ("Peeters") — allebei correct gespeld, maar een kale
    stringvergelijking zag dat toch als mismatch en escaleerde onterecht,
    zelfs als de klant de juiste naam had gegeven."""
    fake_pool.connection.fetchrow.return_value = None
    adapter = _fake_adapter([{"booking_id": "abc", "customer_name": "Peeters", "start": "x", "end": "y"}])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)

    result = await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "cancel_appointment",
        {"booking_id": "abc", "confirmed_customer_name": "P-E-E-T-E-R-S"},
    )

    assert result["status"] == "cancelled"
    adapter.cancel_booking.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_appointment_unknown_booking_id_is_rejected(tenant, fake_pool, monkeypatch):
    fake_pool.connection.fetchrow.return_value = None
    adapter = _fake_adapter([])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)

    result = await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "cancel_appointment",
        {"booking_id": "onbestaand", "confirmed_customer_name": "Jan Peeters"},
    )

    assert "error" in result
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

    result = await agent.execute_tool(
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

    result = await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "book_appointment",
        {"start": "2026-07-07T09:00:00", "end": "2026-07-07T09:30:00", "summary": "Kapbeurt"},
        was_new_at_turn_start=True,
    )

    assert "error" not in result
    adapter.book.assert_awaited_once()


@pytest.mark.asyncio
async def test_book_appointment_refuses_when_slot_already_busy(tenant, fake_pool, monkeypatch):
    """Regressie-test: book_appointment mag nooit enkel op het model vertrouwen
    om eerst check_availability aan te roepen. Een klant die manueel al iets
    in de agenda zet, mag nooit dubbel geboekt worden door Lisa."""
    adapter = _fake_adapter([], busy_periods=[{"busy_start": "2026-07-07T13:00:00+02:00", "busy_end": "2026-07-07T13:30:00+02:00"}])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)
    monkeypatch.setattr(agent, "find_or_flag_new", AsyncMock(return_value=(None, True)))

    result = await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "book_appointment",
        {"start": "2026-07-07T13:00:00", "end": "2026-07-07T13:30:00", "summary": "Kapbeurt"},
        was_new_at_turn_start=True,
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
        was_new_at_turn_start=True,
    )

    assert "error" not in result
    adapter.book.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_availability_converts_utc_busy_periods_to_local_time(tenant, fake_pool, monkeypatch):
    """Regressie-test voor een echte bug (gevonden via live testen): Google's
    freebusy-API geeft busy-tijden altijd in UTC terug, ongeacht de gevraagde
    tijdzone. Zonder conversie naar Europe/Brussels kon het model een volledig
    BEZET lokaal tijdslot als vrij interpreteren (in de winter 1 uur, in de
    zomer 2 uur verschil) en dat ook zo tegen de klant zeggen — een 's zomers
    aangevraagd 14:00-15:00 lokaal tijdslot komt overeen met 12:00-13:00 UTC in
    Google's antwoord; zonder conversie leken die tijden voor het model niet
    overlappend."""
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
async def test_create_customer_refuses_when_dossier_already_exists(tenant, fake_pool, monkeypatch):
    """Regressie-test voor een echte bug: create_customer gebruikt ON CONFLICT
    DO UPDATE, dus zonder deze guard zou het model een BESTAAND dossier stil
    kunnen overschrijven door gewoon een andere naam op te geven — waarna de
    naam-mismatch-check niets meer detecteert (want de database staat dan al
    op de nieuwe, foute naam voor de check ooit plaatsvindt)."""
    adapter = _fake_adapter([])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)
    monkeypatch.setattr(
        agent, "find_or_flag_new",
        AsyncMock(return_value=({"phone_number": "+32470000001", "name": "Darmont"}, False)),
    )
    register_mock = AsyncMock()
    monkeypatch.setattr(agent, "register_new_customer", register_mock)

    result = await agent.execute_tool(
        tenant, fake_pool, "+32470000001", "create_customer", {"name": "Katia"}
    )

    assert result["requires_human"] is True
    register_mock.assert_not_called()


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
        tenant, fake_pool, "+32470000001", "escalate_to_human", {"reason": "klacht"}
    )

    assert result["status"] == "escalated"
    assert result["escalation_contact"] == tenant.escalation_contact


@pytest.mark.asyncio
async def test_unknown_tool_returns_error(tenant, fake_pool, monkeypatch):
    adapter = _fake_adapter([])
    monkeypatch.setattr(agent, "get_integration", lambda t, p: adapter)

    result = await agent.execute_tool(tenant, fake_pool, "+32470000001", "carrier_pigeon", {})

    assert "error" in result
