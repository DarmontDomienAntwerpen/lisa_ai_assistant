import json

import pytest

from config import decrypt_text, encrypt_text
from customer_lookup import find_or_flag_new, local_create_customer, local_lookup_customer, register_new_customer


@pytest.mark.asyncio
async def test_local_lookup_customer_returns_none_when_unknown(fake_pool):
    fake_pool.connection.fetchrow.return_value = None
    result = await local_lookup_customer(fake_pool, "kapper_devries", "+32470000001")
    assert result is None


@pytest.mark.asyncio
async def test_local_lookup_customer_merges_details(fake_pool):
    fake_pool.connection.fetchrow.return_value = {
        "phone_number": "+32470000001",
        "details": encrypt_text(json.dumps({"name": "Jan"})),
    }
    result = await local_lookup_customer(fake_pool, "kapper_devries", "+32470000001")
    assert result == {"phone_number": "+32470000001", "name": "Jan"}


@pytest.mark.asyncio
async def test_local_create_customer_stores_details_encrypted(fake_pool):
    result = await local_create_customer(fake_pool, "kapper_devries", "+32470000001", {"name": "Jan"})
    assert result == {"phone_number": "+32470000001", "name": "Jan"}
    call_args = fake_pool.connection.execute.call_args.args
    assert call_args[1] == "kapper_devries"
    assert call_args[2] == "+32470000001"
    assert json.loads(decrypt_text(call_args[3])) == {"name": "Jan"}


@pytest.mark.asyncio
async def test_find_or_flag_new_reports_new_customer_for_none_tenant(tenant, fake_pool):
    fake_pool.connection.fetchrow.return_value = None
    customer, is_new = await find_or_flag_new(tenant, fake_pool, "+32470000001")
    assert customer is None
    assert is_new is True


@pytest.mark.asyncio
async def test_find_or_flag_new_reports_existing_customer(tenant, fake_pool):
    fake_pool.connection.fetchrow.return_value = {
        "phone_number": "+32470000001",
        "details": encrypt_text(json.dumps({"name": "Jan"})),
    }
    customer, is_new = await find_or_flag_new(tenant, fake_pool, "+32470000001")
    assert customer == {"phone_number": "+32470000001", "name": "Jan"}
    assert is_new is False


@pytest.mark.asyncio
async def test_register_new_customer_includes_phone_number(tenant, fake_pool):
    result = await register_new_customer(tenant, fake_pool, "+32470000001", {"name": "Jan"})
    assert result == {"phone_number": "+32470000001", "name": "Jan"}


@pytest.mark.asyncio
async def test_local_create_customer_never_stores_plaintext_notes(fake_pool):
    """Klantnotities kunnen gezondheidsgegevens bevatten (bv. kinebehandeling) —
    de opgeslagen waarde mag nooit het plaintext bevatten."""
    await local_create_customer(fake_pool, "kine_janssens", "+32470000001", {"notes": "kniepijn na blessure"})
    stored_ciphertext = fake_pool.connection.execute.call_args.args[3]
    assert "kniepijn" not in stored_ciphertext
