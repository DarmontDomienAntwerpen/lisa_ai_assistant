import pytest

from config import encrypt_text
from conversation_store import append_message, get_history


@pytest.mark.asyncio
async def test_get_history_maps_records_to_role_content_dicts(fake_pool):
    fake_pool.connection.fetch.return_value = [
        {"role": "user", "content": encrypt_text("Hoi")},
        {"role": "assistant", "content": encrypt_text("Hallo, waarmee kan ik helpen?")},
    ]
    history = await get_history(fake_pool, "kapper_devries", "+32470000001")
    assert history == [
        {"role": "user", "content": "Hoi"},
        {"role": "assistant", "content": "Hallo, waarmee kan ik helpen?"},
    ]


@pytest.mark.asyncio
async def test_append_message_scopes_to_tenant_and_phone_number(fake_pool):
    await append_message(fake_pool, "kapper_devries", "+32470000001", "whatsapp", "user", "Hoi")
    args = fake_pool.connection.execute.call_args.args
    assert args[1] == "kapper_devries"
    assert args[2] == "+32470000001"
    assert args[3] == "whatsapp"
    assert args[4] == "user"
    assert args[5] != "Hoi"  # content moet versleuteld zijn, niet plat opgeslagen
