"""Pinned direct Anthropic costs, without a paid model or database request."""

from copy import deepcopy

import pytest

from tin_lite.billing_contracts import BillingError, receipt_charge
from tin_lite.service_pricing import CARD, LAUNCH_CARD, model_maximum, service_terms
from tin_lite.studio_billing import CARD as STUDIO_CARD


def terms(sound="music"):
    return service_terms({"executor": "creative.launch_video"}, inputs={"sound_mode": sound})


def receipt(**patch):
    return {
        "provider": "anthropic",
        "model": "claude-opus-5-5",
        "service_tier": "standard",
        "outcome": "response_received",
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "cached_input_tokens": 200,
            "cache_write_input_tokens": 300,
            "cache_write_5m_input_tokens": 100,
            "cache_write_1h_input_tokens": 200,
            "web_search_calls": 0,
        },
        **patch,
    }


def test_launch_quote_pins_anthropic_and_does_not_mutate_the_historical_openai_card():
    old = deepcopy(CARD)
    quoted = terms()
    assert quoted["rate_card"] == LAUNCH_CARD["id"]
    assert quoted["service_pricing"] == LAUNCH_CARD
    assert quoted["maximum_nanos"] == 6_000_000_000
    assert quoted["operations"] == ["native_model"]
    assert quoted["execution_fee_nanos"] == 0
    assert "studio_pricing" not in quoted
    quoted["service_pricing"]["models"].clear()
    assert LAUNCH_CARD["models"]
    assert CARD == old
    assert service_terms({"executor": "creative.character"})["service_pricing"] == old


def test_launch_narration_adds_only_receipted_existing_studio_prices():
    quoted = terms("voice_and_music")
    assert quoted["studio_pricing"] == STUDIO_CARD
    assert quoted["operations"] == ["native_model", "tool"]
    assert (
        receipt_charge(
            quoted,
            "tool",
            {
                "provider": "fal",
                "endpoint": STUDIO_CARD["endpoints"][0],
                "outcome": "cost_received",
                "provider_request_id": "request-1",
                "reported_cost_nanos": 123,
            },
        )[0]
        == 123
    )


def test_launch_prices_observed_cache_ttls_instead_of_assuming_every_write_is_five_minutes():
    # 500 ordinary input + 200 cache reads + 100 five-minute + 200 one-hour writes.
    assert receipt_charge(terms(), "native_model", receipt())[0] == (
        500 * 4000 + 200 * 200 + 100 * 5000 + 200 * 8000 + 100 * 20000
    )
    no_cache = {
        **receipt()["usage"],
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "cache_write_5m_input_tokens": None,
        "cache_write_1h_input_tokens": None,
    }
    assert receipt_charge(terms(), "native_model", receipt(usage=no_cache))[0] == 6_000_000


@pytest.mark.parametrize(
    "patch",
    [
        {"provider": "openrouter"},
        {"provider": "openai"},
        {"model": "claude-sonnet-5-5"},
        {"service_tier": "priority"},
        {"service_tier": None},
        {"outcome": "unconfirmed"},
        {"usage": {**receipt()["usage"], "total_tokens": 2}},
        {"usage": {**receipt()["usage"], "cache_write_1h_input_tokens": None}},
        {"usage": {**receipt()["usage"], "cache_write_5m_input_tokens": 300}},
        {"usage": {**receipt()["usage"], "web_search_calls": 1}},
        {"usage": {**receipt()["usage"], "cached_input_tokens": 900}},
    ],
)
def test_launch_keeps_unknown_or_inconsistent_costs_unpriced(patch):
    assert receipt_charge(terms(), "native_model", receipt(**patch)) is None


def test_launch_reserves_a_conservative_bound_before_dispatch():
    maximum = model_maximum(
        terms(),
        provider="anthropic",
        model="claude-opus-5-5",
        input_tokens=120_000,
        output_tokens=24_000,
    )
    assert maximum == 120_000 * 8000 + 24_000 * 20000
    assert maximum * 3 + 2 * STUDIO_CARD["request_maximum_nanos"] < terms()["maximum_nanos"]
    with pytest.raises(BillingError, match="no pinned workflow price"):
        model_maximum(
            terms(), provider="openrouter", model="claude-opus-5-5", input_tokens=1, output_tokens=1
        )
    with pytest.raises(BillingError, match="no priced search tool"):
        model_maximum(
            terms(),
            provider="anthropic",
            model="claude-opus-5-5",
            input_tokens=1,
            output_tokens=1,
            searches=1,
        )
