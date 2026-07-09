import pytest

from app.tenants import Tenant, get_tenant_by_client_id, get_tenant_by_number, list_tenants


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
        "calendar_type": "none",
        "calendar_config": "{}",
        "system_prompt_extra": "",
        "escalation_contact": "+3247000001",
    }
    result = await get_tenant_by_number(fake_pool, "+3234000001")
    assert isinstance(result, Tenant)
    assert result.client_id == "kapper_devries"
    assert result.calendar_config == {}


@pytest.mark.asyncio
async def test_get_tenant_by_number_queries_on_twilio_number(fake_pool):
    await get_tenant_by_number(fake_pool, "+3234000001")
    args = fake_pool.connection.fetchrow.call_args.args
    assert "+3234000001" in args


@pytest.mark.asyncio
async def test_get_tenant_by_client_id_returns_none_when_not_found(fake_pool):
    fake_pool.connection.fetchrow.return_value = None
    result = await get_tenant_by_client_id(fake_pool, "onbekend")
    assert result is None


@pytest.mark.asyncio
async def test_get_tenant_by_client_id_maps_record_to_tenant(fake_pool):
    fake_pool.connection.fetchrow.return_value = {
        "client_id": "kapper_devries",
        "business_name": "Kapsalon De Vries",
        "niche": "kapper",
        "twilio_number": "+3234000001",
        "calendar_type": "none",
        "calendar_config": "{}",
        "system_prompt_extra": "",
        "escalation_contact": "+3247000001",
    }
    result = await get_tenant_by_client_id(fake_pool, "kapper_devries")
    assert isinstance(result, Tenant)
    assert result.client_id == "kapper_devries"


@pytest.mark.asyncio
async def test_list_tenants_maps_all_records(fake_pool):
    fake_pool.connection.fetch.return_value = [
        {
            "client_id": "kapper_devries",
            "business_name": "Kapsalon De Vries",
            "niche": "kapper",
            "twilio_number": "+3234000001",
            "calendar_type": "none",
            "calendar_config": "{}",
            "system_prompt_extra": "",
            "escalation_contact": "+3247000001",
        }
    ]
    result = await list_tenants(fake_pool)
    assert len(result) == 1
    assert isinstance(result[0], Tenant)
    assert result[0].client_id == "kapper_devries"
