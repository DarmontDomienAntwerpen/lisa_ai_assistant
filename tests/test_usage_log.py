import pytest

from config import MODEL_DEFAULT, MODEL_ESCALATION
from usage_log import calculate_cost_usd


def test_calculate_cost_usd_scales_with_tokens():
    cost_1k = calculate_cost_usd(MODEL_DEFAULT, 1000, 1000)
    cost_2k = calculate_cost_usd(MODEL_DEFAULT, 2000, 2000)
    assert cost_2k == pytest.approx(cost_1k * 2)


def test_calculate_cost_usd_escalation_model_is_pricier_per_token():
    cheap = calculate_cost_usd(MODEL_DEFAULT, 1000, 1000)
    expensive = calculate_cost_usd(MODEL_ESCALATION, 1000, 1000)
    assert expensive > cheap


def test_calculate_cost_usd_unknown_model_defaults_to_zero():
    assert calculate_cost_usd("unknown-model", 1000, 1000) == 0.0
