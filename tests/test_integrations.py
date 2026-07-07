import dataclasses

import httpx
import pytest

from integrations.base import IntegrationError, get_integration
from integrations.generic_rest_api import GenericRestApiIntegration
from integrations.google_calendar import GoogleCalendarIntegration
from integrations.none import NoIntegration
from integrations.outlook_calendar import OutlookCalendarIntegration


def test_get_integration_dispatches_on_calendar_type(tenant, fake_pool):
    assert isinstance(get_integration(tenant, fake_pool), NoIntegration)

    google_tenant = dataclasses.replace(tenant, calendar_type="google_calendar")
    assert isinstance(get_integration(google_tenant, fake_pool), GoogleCalendarIntegration)

    outlook_tenant = dataclasses.replace(tenant, calendar_type="outlook_calendar")
    assert isinstance(get_integration(outlook_tenant, fake_pool), OutlookCalendarIntegration)

    api_tenant = dataclasses.replace(tenant, calendar_type="custom_api", calendar_config={"base_url": "https://x.example.com"})
    assert isinstance(get_integration(api_tenant, fake_pool), GenericRestApiIntegration)


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


@pytest.mark.asyncio
async def test_outlook_integration_raises_not_implemented(tenant, fake_pool):
    adapter = OutlookCalendarIntegration(tenant, fake_pool)
    with pytest.raises(IntegrationError):
        await adapter.check_availability(None, None)
    with pytest.raises(IntegrationError):
        await adapter.find_bookings({"phone_number": "+3247"}, None, None)
    with pytest.raises(IntegrationError):
        await adapter.cancel_booking({"booking_id": "x"}, {"phone_number": "+3247"})
    with pytest.raises(IntegrationError):
        await adapter.reschedule_booking({"booking_id": "x"}, {"start": "a", "end": "b"}, {"phone_number": "+3247"})


def test_generic_rest_api_requires_base_url(tenant, fake_pool):
    api_tenant = dataclasses.replace(tenant, calendar_type="custom_api", calendar_config={})
    with pytest.raises(IntegrationError):
        GenericRestApiIntegration(api_tenant, fake_pool)


@pytest.mark.asyncio
async def test_generic_rest_api_lookup_customer_wraps_transport_errors(tenant, fake_pool, monkeypatch):
    api_tenant = dataclasses.replace(
        tenant, calendar_type="custom_api", calendar_config={"base_url": "https://x.example.com"}
    )
    adapter = GenericRestApiIntegration(api_tenant, fake_pool)

    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return None

        async def request(self, *args, **kwargs):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr("integrations.generic_rest_api.httpx.AsyncClient", lambda **kwargs: FailingClient())

    with pytest.raises(IntegrationError):
        await adapter.lookup_customer("+3247")
