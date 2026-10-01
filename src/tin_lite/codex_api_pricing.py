"""Immutable public-list-price contract for the isolated API credit pilot.

Not a supplier invoice. Never apply this tariff to ChatGPT OAuth observations.
"""

from tin_lite.billing_contracts import NANOS_PER_DOLLAR, digest, token_charge
from tin_lite.codex_api import (
    CONTRACT,
    DIAGRAM_CONTRACT,
    MODE,
    MODEL,
    SESSION_CONTRACT,
    procedure_contract,
)
from tin_lite.workflow_costs import SESSION_FUNDING

RATE_CARD = {
    "id": "openai-codex-standard-2026-09-23-v1",
    "provider": "openai",
    "model": MODEL,
    "service_tier": "default",
    "source": "https://developers.openai.com/api/docs/models/gpt-6-sol",
    "tool_source": "https://developers.openai.com/api/docs/pricing",
    "cache_source": "https://developers.openai.com/api/docs/guides/prompt-caching",
    "unit": "USD_nanodollars_per_token",
    "standard": {"input": 2_000, "cached_input": 200, "cache_write": 2_500, "output": 10_000},
    "long_context_above_input_tokens": 272_000,
    "long_context": {
        "input": 4_000,
        "cached_input": 400,
        "cache_write": 5_000,
        "output": 15_000,
    },
    "web_search_call_nanos": 10_000_000,
}
# Cards earlier runs pinned. They still price those runs' outstanding receipts;
# new runs never select them.
HISTORICAL_RATE_CARDS = (
    {
        "id": "openai-codex-standard-2026-09-12-v1",
        "provider": "openai",
        "model": "gpt-6-astra",
        "service_tier": "default",
        "source": "https://developers.openai.com/api/docs/models/gpt-6-astra",
        "tool_source": "https://developers.openai.com/api/docs/pricing",
        "cache_source": "https://developers.openai.com/api/docs/guides/prompt-caching",
        "unit": "USD_nanodollars_per_token",
        "standard": {
            "input": 10_000,
            "cached_input": 1_000,
            "cache_write": 12_500,
            "output": 50_000,
        },
        "long_context_above_input_tokens": 272_000,
        "long_context": {
            "input": 20_000,
            "cached_input": 2_000,
            "cache_write": 25_000,
            "output": 75_000,
        },
        "web_search_call_nanos": 10_000_000,
    },
)
# Reserve the entire pilot input envelope, not a guess at its cache-hit rate.
# This is a customer liability ceiling, not a promise that the supplier cannot
# exceed it (e.g. hidden search context). Tin absorbs that excess and stops calls.
REQUEST_INPUT_ENVELOPE = CONTRACT["max_observed_tokens"]
REQUEST_MAXIMUM = (
    REQUEST_INPUT_ENVELOPE * RATE_CARD["standard"]["cache_write"]
    + CONTRACT["max_output_tokens"] * RATE_CARD["standard"]["output"]
    + RATE_CARD["web_search_call_nanos"]
)


# Session ceilings below the default $5, by the procedure's output contract. A page refresh
# reads the page's current text, a few positioning files and the style guide, then writes one
# short document: about 150,000 input and 6,000 output tokens at list price, roughly $0.45.
# Its ceiling is about five times that. No refresh has run yet; recalibrate from measurements.
PROCEDURE_MAXIMUMS = {"content-refresh.v1": 2_500_000_000}


def api_terms(definition, *, session_budget=False):
    validator = definition.get("procedure", {}).get("output", {}).get("validator")
    terms = {
        "rate_card": RATE_CARD["id"],
        "pricing": RATE_CARD,
        "mode": "test",
        "currency": "USD",
        "definition_sha256": digest(definition),
        "kind": "codex_api",
        "codex_auth": MODE,
        "maximum_nanos": PROCEDURE_MAXIMUMS.get(validator, 5 * NANOS_PER_DOLLAR),
        "request_maximum_nanos": REQUEST_MAXIMUM,
        "request_maximum_input_bytes": REQUEST_INPUT_ENVELOPE,
        "execution_fee_nanos": 0,
        "sandbox_second_nanos": 0,
        "rounding": "half_up_cent_at_root_settlement",
        "failure_policy": "verified_usage; platform_duplicates_and_overages_absorbed",
        "unknown_policy": "pending_up_to_24h_then_unresolved_cost_absorbed",
    }
    # Isolated procedures (e.g. onboarding children) joined the procedure contract on
    # 2026-09-25. Terms already pinned without codex_contract keep the v1 CONTRACT.
    if definition.get("procedure", {}).get("sandbox", {}).get("profile", "default") in {
        "default",
        "isolated",
        "browser",
        "studio",
    }:
        contract = procedure_contract(
            definition.get("procedure", {}).get("output", {}).get("validator")
        )
        terms.update(
            codex_contract=contract,
            request_maximum_input_bytes=contract["max_request_bytes"],
            request_maximum_nanos=(
                contract["context_window"] * RATE_CARD["standard"]["cache_write"]
                + contract["max_output_tokens"] * RATE_CARD["standard"]["output"]
                + RATE_CARD["web_search_call_nanos"]
            ),
        )
    if definition.get("procedure", {}).get("sandbox", {}).get("profile") == "studio":
        from tin_lite.studio_billing import CARD

        terms.update(studio_pricing=CARD, operations=["codex_api", "tool"])
    procedure = definition.get("procedure", {})
    if (
        session_budget
        and definition.get("executor") == "codex.procedure"
        and procedure.get("sandbox", {}).get("profile", "default")
        in {"default", "isolated", "browser"}
        and procedure_contract(procedure.get("output", {}).get("validator")) != DIAGRAM_CONTRACT
    ):
        terms.update(
            funding=SESSION_FUNDING,
            codex_contract=SESSION_CONTRACT,
            request_maximum_input_bytes=SESSION_CONTRACT["max_request_bytes"],
        )
        terms.pop("request_maximum_nanos")
    return terms


def isolated_v1_terms(terms):
    """The pilot v1 shape isolated procedures were quoted with before 2026-09-25.

    Only for honoring an already-issued, unexpired quote; new admissions use v3.
    """
    terms = {key: value for key, value in terms.items() if key != "codex_contract"}
    terms.update(
        request_maximum_input_bytes=REQUEST_INPUT_ENVELOPE,
        request_maximum_nanos=REQUEST_MAXIMUM,
    )
    return terms


def issued_before_isolated_v3(quoted_terms, terms, definition):
    return (
        quoted_terms.get("kind") == "codex_api"
        and "codex_contract" not in quoted_terms
        and "codex_contract" in terms
        and definition.get("procedure", {}).get("sandbox", {}).get("profile") == "isolated"
    )


def price_response(card, record):
    """Only complete, internally consistent supplier facts admit a price."""
    usage = record.get("usage")
    if (
        (card != RATE_CARD and card not in HISTORICAL_RATE_CARDS)
        or record.get("pricing") != card
        or record.get("provider") != card["provider"]
        or record.get("model") != card["model"]
        or record.get("service_tier") != card["service_tier"]
        or record.get("outcome") != "response_received"
        or record.get("endpoint") != "responses"
        or not isinstance(usage, dict)
    ):
        return None
    inputs, outputs, total, searches = (
        usage.get(key)
        for key in ("input_tokens", "output_tokens", "total_tokens", "web_search_calls")
    )
    if any(type(n) is not int or n < 0 for n in (inputs, outputs, total, searches)):
        return None
    if total != inputs + outputs:
        return None
    band = "long_context" if inputs > card["long_context_above_input_tokens"] else "standard"
    model_nanos = token_charge({"codex_api": card[band]}, "codex_api", usage)
    if model_nanos is None:
        return None
    tool_nanos = searches * card["web_search_call_nanos"]
    return model_nanos + tool_nanos, {
        "rate_card": card["id"],
        "basis": "published_api_list_price",
        "provider": card["provider"],
        "model": card["model"],
        "service_tier": card["service_tier"],
        "context_band": band,
        "usage": usage,
        "model_nanos": model_nanos,
        "tool_nanos": tool_nanos,
        "outcome": record["outcome"],
    }
