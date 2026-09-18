"""Pinned supplier-price terms for trusted native workflow calls.

This is separate from historical illustrative tariffs and Codex OAuth counters.
Prices are USD nanodollars; never round individual calls. Unknown models are not
aliases for a known price. Adding a provider adapter does not select a paid route.
"""

from copy import deepcopy
from decimal import Decimal, InvalidOperation

from tin_lite.billing_contracts import NANOS_PER_DOLLAR, BillingError, digest, token_charge

CARD = {
    "id": "tin-native-supplier-2026-09-14-v1",
    "source": "https://developers.openai.com/api/docs/pricing",
    "provider": "openai",
    "service_tier": "default",
    "long_context_above_input_tokens": 272_000,
    "models": {
        "gpt-5.6-luna": {
            "standard": {"input": 200, "cached_input": 20, "cache_write": 250, "output": 1200},
            "long_context": {"input": 400, "cached_input": 40, "cache_write": 500, "output": 1800},
        },
        "gpt-6-astra": {
            "standard": {
                "input": 10_000,
                "cached_input": 1000,
                "cache_write": 12_500,
                "output": 50_000,
            },
            "long_context": {
                "input": 20_000,
                "cached_input": 2000,
                "cache_write": 25_000,
                "output": 75_000,
            },
        },
    },
    "web_search_call_nanos": 10_000_000,
    "tools": {"dataforseo": "provider_reported_task_cost_usd"},
}

NATIVE_EXECUTORS = {
    "content.plan",
    "creative.character",
    "style.capture",
    "content.answer_page",
    "project.memory",
    "project.weekly_brief",
    "scan.report",
    "visibility.audit",
    "organic.audit",
    "organic.keyword_plan",
    "growth.onboarding_plan",
}
PARENT_EXECUTORS = {"organic.traffic_system", "growth.onboarding"}


def amount_nanos(value):
    """Supplier precision is not restricted to the two-decimal checkout input."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 <= number <= 100_000:
            return None
        nanos = number * NANOS_PER_DOLLAR
        return int(nanos) if nanos == nanos.to_integral_value() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def service_terms(definition, *, inputs=None):
    executor = definition.get("executor")
    if executor not in NATIVE_EXECUTORS | PARENT_EXECUTORS:
        return None
    inputs = inputs or {}
    maximum = 2 * NANOS_PER_DOLLAR
    kinds = ["native_model"]
    if executor == "organic.audit":
        maximum, kinds = 5 * NANOS_PER_DOLLAR, ["native_model", "tool"]
    elif executor == "organic.keyword_plan":
        maximum = amount_nanos(inputs.get("max_cost_usd", 9))
        kinds = ["native_model", "tool"]
    elif executor == "organic.traffic_system":
        maximum = amount_nanos(inputs.get("keyword_max_cost_usd", 9))
        if maximum is not None:
            maximum += (7 + (5 if inputs.get("technical_fix") else 0)) * NANOS_PER_DOLLAR
            if definition.get("organic_system_policy", {}).get("version") == "organic-traffic-v2":
                # One draft and, unless explicitly disabled, one repository adaptation.
                # This is a bound, not an upfront charge or six-month reservation.
                maximum += (
                    5 + (5 if inputs.get("content_delivery", "auto") == "auto" else 0)
                ) * NANOS_PER_DOLLAR
        kinds = []  # The parent itself never buys a model call.
    elif executor == "growth.onboarding":
        maximum, kinds = 10 * NANOS_PER_DOLLAR, []
    elif executor == "growth.onboarding_plan":
        # About twenty-five bounded model steps; each reserves its conservative ceiling first.
        maximum = 8 * NANOS_PER_DOLLAR
    if type(maximum) is not int or maximum <= 0:
        raise BillingError("invalid_budget", "The workflow spending maximum is invalid.")
    return {
        "rate_card": CARD["id"],
        "service_pricing": deepcopy(CARD),
        "mode": "test",
        "currency": "USD",
        "definition_sha256": digest(definition),
        "kind": "parent" if executor in PARENT_EXECUTORS else "metered_workflow",
        "operations": kinds,
        "maximum_nanos": maximum,
        "execution_fee_nanos": 0,
        "rounding": "half_up_cent_at_root_settlement",
        "failure_policy": "verified_usage; platform_duplicates_and_overages_absorbed",
        "unknown_policy": "pending_up_to_24h_then_unresolved_cost_absorbed",
    }


def model_card(terms, provider, model):
    card = terms.get("service_pricing")
    if not card or provider != card["provider"] or model not in card["models"]:
        raise BillingError("unpriced_model", "This model has no pinned workflow price.")
    return card, card["models"][model]


def model_maximum(terms, *, provider, model, input_tokens, output_tokens, searches=0):
    if "service_pricing" not in terms:  # Historical test quotes keep their meaning.
        return token_charge(
            terms,
            "native_model",
            {
                "input_tokens": input_tokens,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": output_tokens,
            },
        )
    card, rates = model_card(terms, provider, model)
    band = "long_context" if input_tokens > card["long_context_above_input_tokens"] else "standard"
    rate = rates[band]
    return (
        input_tokens * max(rate["input"], rate["cache_write"])
        + output_tokens * rate["output"]
        + searches * card["web_search_call_nanos"]
    )


def price_receipt(terms, kind, record):
    if record.get("outcome") not in {"response_received", "invalid_output"}:
        return None
    card = terms["service_pricing"]
    if kind == "tool":
        if record.get("provider") not in card["tools"] or record.get("category") != "tool":
            return None
        amount = amount_nanos(record.get("reported_cost_usd"))
        return (
            None
            if amount is None
            else (
                amount,
                {
                    "provider": record["provider"],
                    "endpoint": record["endpoint"],
                    "basis": "provider_reported_cost",
                    "reported_cost_usd": record["reported_cost_usd"],
                },
            )
        )
    if kind != "native_model" or record.get("service_tier") != card["service_tier"]:
        return None
    try:
        _, rates = model_card(terms, record.get("provider"), record.get("model"))
    except BillingError:
        return None
    usage = record.get("usage")
    if not isinstance(usage, dict):
        return None
    inputs, outputs, total, searches = (
        usage.get(k)
        for k in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "web_search_calls",
        )
    )
    searches = usage.get("billable_web_search_calls", searches)
    waived_searches = usage.get("unpriced_web_search_actions", 0)
    if searches is None and "billable_web_search_calls" in usage:
        # Older completed observations kept an all-or-nothing search count. Their
        # model tokens are still verified. Waive those search fees, not the user's
        # available credits; never invent a supplier cost or mutate its receipt.
        count = usage.get("web_search_calls")
        if type(count) is int and count >= 0:
            searches, waived_searches = 0, count
    if (
        any(type(n) is not int or n < 0 for n in (inputs, outputs, total, searches))
        or total != inputs + outputs
    ):
        return None
    band = "long_context" if inputs > card["long_context_above_input_tokens"] else "standard"
    amount = token_charge({"native_model": rates[band]}, "native_model", usage)
    if amount is None:
        return None
    return amount + searches * card["web_search_call_nanos"], {
        "rate_card": card["id"],
        "provider": record["provider"],
        "model": record["model"],
        "basis": "published_api_list_price",
        "context_band": band,
        "unpriced_search_fees_absorbed": waived_searches,
        "usage": usage,
        "outcome": record["outcome"],
    }
