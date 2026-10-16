import json

import pytest

from app.config import decrypt_text, encrypt_text
from app.customer_lookup import find_or_flag_new, local_create_customer, local_lookup_customer, register_new_customer


@pytest.mark.asyncio
async def test_local_lookup_customer_returns_none_when_unknown(fake_pool):
    fake_pool.connection.fetch.return_value = []
    result = await local_lookup_customer(fake_pool, "kapper_devries", "+32470000001")
    assert result is None


@pytest.mark.asyncio
async def test_local_lookup_customer_merges_details(fake_pool):
    fake_pool.connection.fetch.return_value = [
        {"id": 1, "phone_number": "+32470000001", "details": encrypt_text(json.dumps({"name": "Jan"}))}
    ]
    result = await local_lookup_customer(fake_pool, "kapper_devries", "+32470000001")
    assert result == {"phone_number": "+32470000001", "name": "Jan"}


@pytest.mark.asyncio
async def test_local_lookup_customer_returns_most_recent_when_number_shared(fake_pool):
    """A shared number (family) can have several people - the 'best guess'
    for places that expect only one customer is the most recent one
    (local_list_customers already sorts DESC on created_at)."""
    fake_pool.connection.fetch.return_value = [
        {"id": 2, "phone_number": "+32470000001", "details": encrypt_text(json.dumps({"name": "Marie"}))},
        {"id": 1, "phone_number": "+32470000001", "details": encrypt_text(json.dumps({"name": "Jan"}))},
    ]
    result = await local_lookup_customer(fake_pool, "kapper_devries", "+32470000001")
    assert result == {"phone_number": "+32470000001", "name": "Marie"}
    assert "_id" not in result


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
    fake_pool.connection.fetch.return_value = []
    customer, is_new = await find_or_flag_new(tenant, fake_pool, "+32470000001")
    assert customer is None
    assert is_new is True


@pytest.mark.asyncio
async def test_find_or_flag_new_reports_existing_customer(tenant, fake_pool):
    fake_pool.connection.fetch.return_value = [
        {"id": 1, "phone_number": "+32470000001", "details": encrypt_text(json.dumps({"name": "Jan"}))}
    ]
    customer, is_new = await find_or_flag_new(tenant, fake_pool, "+32470000001")
    assert customer == {"phone_number": "+32470000001", "name": "Jan"}
    assert is_new is False


@pytest.mark.asyncio
async def test_register_new_customer_includes_phone_number(tenant, fake_pool):
    result = await register_new_customer(tenant, fake_pool, "+32470000001", {"name": "Jan"})
    assert result == {"phone_number": "+32470000001", "name": "Jan"}


@pytest.mark.asyncio
async def test_local_create_customer_inserts_new_row_for_different_name_on_same_number(fake_pool):
    """Regression test: a shared number (family) must be able to hold several
    people. Jan is already on this number -- Marie (other name, same number)
    must become a NEW row, not an overwrite."""
    fake_pool.connection.fetch.return_value = [
        {"id": 1, "phone_number": "+32470000001", "details": encrypt_text(json.dumps({"name": "Jan"}))}
    ]
    await local_create_customer(fake_pool, "kapper_devries", "+32470000001", {"name": "Marie"})

    sql = fake_pool.connection.execute.call_args.args[0]
    assert "INSERT INTO customers" in sql
    assert "UPDATE" not in sql


@pytest.mark.asyncio
async def test_local_create_customer_updates_existing_row_for_same_name(fake_pool):
    """If Jan calls again and goes through intake again (e.g. email added),
    HIS row is updated; no duplicate row is created."""
    fake_pool.connection.fetch.return_value = [
        {"id": 1, "phone_number": "+32470000001", "details": encrypt_text(json.dumps({"name": "Jan"}))}
    ]
    await local_create_customer(fake_pool, "kapper_devries", "+32470000001", {"name": "Jan", "email": "jan@test.be"})

    sql, new_details, row_id = fake_pool.connection.execute.call_args.args
    assert "UPDATE customers" in sql
    assert row_id == 1
    assert json.loads(decrypt_text(new_details)) == {"name": "Jan", "email": "jan@test.be"}


@pytest.mark.asyncio
async def test_local_create_customer_never_overwrites_existing_nonempty_fields(fake_pool):
    """Regression test (security review): a chance name match (misheard, or
    just the same first name as another customer) must NEVER silently erase
    or replace Jan's existing notes/email. Only fields that were still empty
    may be filled in."""
    fake_pool.connection.fetch.return_value = [
        {"id": 1, "phone_number": "+32470000001", "details": encrypt_text(json.dumps({
            "name": "Jan", "email": "echte-jan@test.be", "notes": "kniepijn na blessure",
        }))}
    ]
    await local_create_customer(
        fake_pool, "kapper_devries", "+32470000001",
        {"name": "Jan", "email": "andere-email@test.be", "notes": "nieuwe, mogelijk foute notitie"},
    )

    sql, new_details, row_id = fake_pool.connection.execute.call_args.args
    assert "UPDATE customers" in sql
    stored = json.loads(decrypt_text(new_details))
    assert stored["email"] == "echte-jan@test.be"  # not overwritten
    assert stored["notes"] == "kniepijn na blessure"  # not overwritten


@pytest.mark.asyncio
async def test_local_create_customer_never_stores_plaintext_notes(fake_pool):
    """Customer notes can contain health data (e.g. physiotherapy treatment) -
    the stored value must never contain the plaintext."""
    await local_create_customer(fake_pool, "kine_janssens", "+32470000001", {"notes": "kniepijn na blessure"})
    stored_ciphertext = fake_pool.connection.execute.call_args.args[3]
    assert "kniepijn" not in stored_ciphertext
