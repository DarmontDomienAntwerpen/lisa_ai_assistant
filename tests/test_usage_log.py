from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.config import OPENAI_REALTIME_MODEL
from app.usage_log import (
    calculate_cost_usd,
    get_calls_with_cost_breakdown,
    get_tenant_usage_summary,
    log_call_start,
    twilio_cost_usd,
)


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


def test_calculate_cost_usd_cached_input_is_much_cheaper():
    """Regression test for a real overestimate: a Realtime session resends the
    full audio history as input on every turn, but OpenAI caches it
    automatically at a ~80x lower rate. Without accounting for this, a short
    12-turn test conversation seemed to cost $1.38, while much of it was
    cached (and so far cheaper)."""
    fully_fresh = calculate_cost_usd(OPENAI_REALTIME_MODEL, 10_000, 0, cached_input_tokens=0)
    fully_cached = calculate_cost_usd(OPENAI_REALTIME_MODEL, 10_000, 0, cached_input_tokens=10_000)
    assert fully_cached < fully_fresh / 10  # cache rate ($0.40/1M) << full rate ($32/1M)


@pytest.mark.asyncio
async def test_log_call_start_inserts_into_calls_table_and_returns_id(fake_pool):
    fake_pool.connection.fetchval.return_value = 42

    call_id = await log_call_start(fake_pool, "kapper_devries", "+32470000001", "voice")

    args = fake_pool.connection.fetchval.call_args.args
    assert "INSERT INTO calls" in args[0]
    assert "RETURNING id" in args[0]
    assert args[1] == "kapper_devries"
    assert args[2] == "+32470000001"
    assert args[3] == "voice"
    assert call_id == 42


@pytest.mark.asyncio
async def test_get_tenant_usage_summary_merges_usage_and_call_stats(fake_pool):
    """Regression test: usage_log logs per turn (several rows per call),
    calls logs one row per session set up - the dashboard needs both
    (call_count comes from calls, not from usage_log)."""
    fake_pool.connection.fetchrow.side_effect = [
        {"conversation_turns": 5, "total_input_tokens": 100, "total_output_tokens": 200, "total_cost_usd": 0.05, "escalations": 1},
        {"call_count": 2, "last_call_at": None, "total_duration_seconds": 120},
    ]
    summary = await get_tenant_usage_summary(fake_pool, "kapper_devries")
    assert summary["conversation_turns"] == 5
    assert summary["call_count"] == 2


def test_twilio_cost_usd_accepts_decimal_duration_from_asyncpg():
    """Regression test for a real production crash: asyncpg returns
    EXTRACT(EPOCH FROM ...) as decimal.Decimal, not float - a bare
    `duration_seconds / 60 * TWILIO_PER_MINUTE_USD` crashes on that
    (TypeError: unsupported operand type(s) for *: 'Decimal' and 'float'),
    which crashed the tenant detail page in production (HTTP 500)."""
    cost = twilio_cost_usd(Decimal("125.500000"))
    assert isinstance(cost, float)
    assert cost > 0


@pytest.mark.asyncio
async def test_get_tenant_usage_summary_handles_decimal_from_real_db_types(fake_pool):
    """Same regression as above, but via the full path: the calls_record
    coming from a real Postgres query holds Decimal for
    total_duration_seconds - get_tenant_usage_summary must not crash on that
    when computing total_twilio_cost_usd."""
    fake_pool.connection.fetchrow.side_effect = [
        {"conversation_turns": 1, "total_input_tokens": 10, "total_output_tokens": 10, "total_cost_usd": Decimal("0.01"), "escalations": 0},
        {"call_count": 1, "last_call_at": None, "total_duration_seconds": Decimal("90.000000")},
    ]
    summary = await get_tenant_usage_summary(fake_pool, "kapper_devries")
    assert summary["total_twilio_cost_usd"] > 0


@pytest.mark.asyncio
async def test_get_calls_with_cost_breakdown_handles_decimal_from_real_db_types(fake_pool):
    """Same regression for the per-call breakdown the dashboard expands -
    c.ended_at - c.started_at also comes back as Decimal from a real Postgres
    row via EXTRACT(EPOCH ...)."""
    fake_pool.connection.fetch.return_value = [
        {
            "call_id": 1,
            "phone_number": "+32470000001",
            "channel": "voice",
            "started_at": datetime(2026, 7, 9, 10, 0, tzinfo=timezone.utc),
            "ended_at": datetime(2026, 7, 9, 10, 2, tzinfo=timezone.utc),
            "duration_seconds": Decimal("120.000000"),
            "turns": 4,
            "total_input_tokens": 1000,
            "total_cached_input_tokens": 500,
            "total_output_tokens": 200,
            "total_cost_usd": Decimal("0.045000"),
            "model": OPENAI_REALTIME_MODEL,
            "escalated": False,
        }
    ]
    calls = await get_calls_with_cost_breakdown(fake_pool, "kapper_devries")
    assert calls[0]["twilio_cost_usd"] > 0
    assert calls[0]["combined_total_cost_usd"] > calls[0]["cost_breakdown"]["total_cost_usd"]
