import pytest

import tenants
from tenants import Tenant, _normalize_number, get_tenant_by_number


def test_normalize_number_strips_whatsapp_prefix():
    assert _normalize_number("whatsapp:+3234000001") == "+3234000001"
    assert _normalize_number("+3234000001") == "+3234000001"


@pytest.mark.asyncio
async def test_get_tenant_by_number_returns_none_when_not_found(fake_pool):
    fake_pool.connection.fetchrow.return_value = None
    result = await get_tenant_by_number(fake_pool, "+3299999999")
    assert result is None


@pytest.mark.asyncio
async def test_get_tenant_by_number_maps_record_to_tenant(fake_pool):
    fake_pool.connection.fetchrow.return_value = {
        "client_id": "kapper_devries",
        "business_name": "Kapsalon De Vries",
        "niche": "kapper",
        "twilio_number": "+3234000001",
        "whatsapp_number": "whatsapp:+3234000001",
        "calendar_type": "none",
        "calendar_config": "{}",
        "system_prompt_extra": "",
        "escalation_contact": "+3247000001",
    }
    result = await get_tenant_by_number(fake_pool, "whatsapp:+3234000001")
    assert isinstance(result, Tenant)
    assert result.client_id == "kapper_devries"
    assert result.calendar_config == {}


@pytest.mark.asyncio
async def test_get_tenant_by_number_queries_both_raw_and_normalized(fake_pool):
    await get_tenant_by_number(fake_pool, "whatsapp:+3234000001")
    args = fake_pool.connection.fetchrow.call_args.args
    assert "whatsapp:+3234000001" in args
    assert "+3234000001" in args
