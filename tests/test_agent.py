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
async def test_cancel_appointment_succeeds_based_on_phone_number_alone(tenant, fake_pool, monkeypatch):
    """Identiteit komt uit het telefoonnummer waarmee het gesprek binnenkomt —
    geen naam-bevestiging meer nodig om te annuleren."""
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
    """Identiteit komt uit het telefoonnummer — boeken voor een bestaande
    klant vereist geen aparte naam-bevestiging meer."""
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
    """Regressie-test voor een echte bug (gevonden via live testen tegen een
    echte agenda): een gedeeld telefoonnummer (bv. gezin) had één dossier op
    naam van de eerste beller (Jan) — toen een TWEEDE gezinslid (Marie) zich
    zelf voorstelde en boekte, kwam de afspraak toch op naam van Jan in de
    agenda, want book_appointment gebruikte altijd de dossiernaam i.p.v. wat
    de beller net zelf zei. caller_name moet nu voorrang krijgen."""
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
    """Zegt de beller dit gesprek geen naam (niet nodig bij een duidelijke,
    korte vraag), dan blijft de gekende dossiernaam gebruikt."""
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
    """Regressie-test: book_appointment mag nooit enkel op het model vertrouwen
    om eerst check_availability aan te roepen. Een klant die manueel al iets
    in de agenda zet, mag nooit dubbel geboekt worden door Lisa."""
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
    kunnen overschrijven door gewoon een andere naam op te geven."""
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
