import pytest

from config import OPENAI_REALTIME_MODEL
from usage_log import calculate_cost_usd, get_tenant_usage_summary, log_call_start


def test_calculate_cost_usd_scales_with_tokens():
    cost_1k = calculate_cost_usd(OPENAI_REALTIME_MODEL, 1000, 1000)
    cost_2k = calculate_cost_usd(OPENAI_REALTIME_MODEL, 2000, 2000)
    assert cost_2k == pytest.approx(cost_1k * 2)


def test_calculate_cost_usd_output_tokens_are_pricier_per_token():
    cost_input_only = calculate_cost_usd(OPENAI_REALTIME_MODEL, 1000, 0)
    cost_output_only = calculate_cost_usd(OPENAI_REALTIME_MODEL, 0, 1000)
    assert cost_output_only > cost_input_only


def test_calculate_cost_usd_unknown_model_defaults_to_zero():
    assert calculate_cost_usd("unknown-model", 1000, 1000) == 0.0


@pytest.mark.asyncio
async def test_log_call_start_inserts_into_calls_table(fake_pool):
    await log_call_start(fake_pool, "kapper_devries", "+32470000001", "voice")
    args = fake_pool.connection.execute.call_args.args
    assert "INSERT INTO calls" in args[0]
    assert args[1] == "kapper_devries"
    assert args[2] == "+32470000001"
    assert args[3] == "voice"


@pytest.mark.asyncio
async def test_get_tenant_usage_summary_merges_usage_and_call_stats(fake_pool):
    """Regressie-test: usage_log logt per gespreksbeurt (meerdere rijen per
    call), calls logt één rij per opgezette sessie — het dashboard heeft
    beide nodig (call_count komt uit calls, niet uit usage_log)."""
    fake_pool.connection.fetchrow.side_effect = [
        {"conversation_turns": 5, "total_input_tokens": 100, "total_output_tokens": 200, "total_cost_usd": 0.05, "escalations": 1},
        {"call_count": 2, "last_call_at": None},
    ]
    summary = await get_tenant_usage_summary(fake_pool, "kapper_devries")
    assert summary["conversation_turns"] == 5
    assert summary["call_count"] == 2
    assert summary["escalations"] == 1
