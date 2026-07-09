import dataclasses

import pytest

from app.integrations.base import get_integration
from app.integrations.google_calendar import GoogleCalendarIntegration
from app.integrations.none import NoIntegration


def test_get_integration_dispatches_on_calendar_type(tenant, fake_pool):
    assert isinstance(get_integration(tenant, fake_pool), NoIntegration)

    google_tenant = dataclasses.replace(tenant, calendar_type="google_calendar")
    assert isinstance(get_integration(google_tenant, fake_pool), GoogleCalendarIntegration)


def test_get_integration_raises_on_unknown_type(tenant, fake_pool):
    bad_tenant = dataclasses.replace(tenant, calendar_type="carrier_pigeon")
    with pytest.raises(ValueError):
        get_integration(bad_tenant, fake_pool)


@pytest.mark.asyncio
async def test_none_integration_book_returns_manual_follow_up(tenant, fake_pool):
    adapter = NoIntegration(tenant, fake_pool)
    result = await adapter.book({"phone_number": "+3247"}, {"start": "x", "end": "y"}, {"summary": "Knipbeurt"})
    assert result["status"] == "manual_follow_up_required"


@pytest.mark.asyncio
async def test_none_integration_check_availability_is_empty(tenant, fake_pool):
    adapter = NoIntegration(tenant, fake_pool)
    assert await adapter.check_availability(None, None) == []


@pytest.mark.asyncio
async def test_none_integration_find_bookings_is_empty(tenant, fake_pool):
    adapter = NoIntegration(tenant, fake_pool)
    assert await adapter.find_bookings({"phone_number": "+3247"}, None, None) == []


@pytest.mark.asyncio
async def test_none_integration_cancel_and_reschedule_require_manual_follow_up(tenant, fake_pool):
    adapter = NoIntegration(tenant, fake_pool)
    cancel_result = await adapter.cancel_booking({"booking_id": "x"}, {"phone_number": "+3247"})
    reschedule_result = await adapter.reschedule_booking({"booking_id": "x"}, {"start": "a", "end": "b"}, {"phone_number": "+3247"})
    assert cancel_result["status"] == "manual_follow_up_required"
    assert reschedule_result["status"] == "manual_follow_up_required"
