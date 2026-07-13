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
    """Een gedeeld nummer (gezin) kan meerdere personen hebben — de 'beste
    gok' voor plekken die maar één klant verwachten is de meest recente
    (local_list_customers sorteert al DESC op created_at)."""
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
    """Regressie-test: een gedeeld nummer (gezin) moet meerdere personen
    kunnen bevatten. Jan staat al op dit nummer -- Marie (andere naam,
    zelfde nummer) moet een NIEUWE rij worden, geen overschrijving."""
    fake_pool.connection.fetch.return_value = [
        {"id": 1, "phone_number": "+32470000001", "details": encrypt_text(json.dumps({"name": "Jan"}))}
    ]
    await local_create_customer(fake_pool, "kapper_devries", "+32470000001", {"name": "Marie"})

    sql = fake_pool.connection.execute.call_args.args[0]
    assert "INSERT INTO customers" in sql
    assert "UPDATE" not in sql


@pytest.mark.asyncio
async def test_local_create_customer_updates_existing_row_for_same_name(fake_pool):
    """Belt Jan nog eens en wordt zijn intake opnieuw doorlopen (bv. e-mail
    toegevoegd), dan wordt ZIJN rij bijgewerkt, geen dubbele rij aangemaakt."""
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
    """Regressie-test (security-review): een toevallige naam-match (verkeerd
    verstaan, of gewoon dezelfde voornaam als een andere klant) mag NOOIT
    Jan's bestaande notities/e-mail stilzwijgend wissen of vervangen. Enkel
    velden die nog leeg waren, mogen aangevuld worden."""
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
    assert stored["email"] == "echte-jan@test.be"  # niet overschreven
    assert stored["notes"] == "kniepijn na blessure"  # niet overschreven


@pytest.mark.asyncio
async def test_local_create_customer_never_stores_plaintext_notes(fake_pool):
    """Klantnotities kunnen gezondheidsgegevens bevatten (bv. kinebehandeling) —
    de opgeslagen waarde mag nooit het plaintext bevatten."""
    await local_create_customer(fake_pool, "kine_janssens", "+32470000001", {"notes": "kniepijn na blessure"})
    stored_ciphertext = fake_pool.connection.execute.call_args.args[3]
    assert "kniepijn" not in stored_ciphertext
