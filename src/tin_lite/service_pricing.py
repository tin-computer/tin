"""Pinned supplier-price terms for trusted native workflow calls.

This is separate from historical illustrative tariffs and Codex OAuth counters.
Prices are USD nanodollars; never round individual calls. Unknown models are not
aliases for a known price. Adding a provider adapter does not select a paid route.
"""

from copy import deepcopy
from decimal import Decimal, InvalidOperation

from tin_lite.billing_contracts import NANOS_PER_DOLLAR, BillingError, digest, token_charge

# OpenAI's standard-tier list prices, checked against the pricing page on 2026-11-04. A run
# pins this card in its terms, so a price change is a new card id and earlier runs keep theirs.
# gpt-6-sol stays for runs and native routes that name it; gpt-6.1-sol is the same price with a
# cheaper cached read. Cache writes are the 30-minute write price.
# v3 adds `other_providers`: each one a provider's own source, tier, long-context threshold
# and models, read by provider_card(). Cards pinned before v3 price OpenAI only.
CARD = {
    "id": "tin-native-supplier-2026-11-23-v3",
    "source": "https://developers.openai.com/api/docs/pricing",
    "provider": "openai",
    "service_tier": "default",
    "long_context_above_input_tokens": 272_000,
    "models": {
        "gpt-6-luna": {
            "standard": {"input": 100, "cached_input": 10, "cache_write": 125, "output": 500},
            "long_context": {"input": 200, "cached_input": 20, "cache_write": 250, "output": 750},
        },
        "gpt-6-sol": {
            "standard": {"input": 2000, "cached_input": 200, "cache_write": 2500, "output": 10_000},
            "long_context": {
                "input": 4000,
                "cached_input": 400,
                "cache_write": 5000,
                "output": 15_000,
            },
        },
        "gpt-6.1-sol": {
            "standard": {"input": 2000, "cached_input": 100, "cache_write": 2500, "output": 10_000},
            "long_context": {
                "input": 4000,
                "cached_input": 200,
                "cache_write": 5000,
                "output": 15_000,
            },
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
    # Gemini's Standard-tier list prices, checked against the pricing page on 2026-11-23. Neon
    # has one price across its 1M context and implicit caching only, so there are no cache
    # writes and no long-context band. Output includes thinking tokens.
    "other_providers": {
        "gemini": {
            "source": "https://ai.google.dev/gemini-api/docs/pricing",
            "service_tier": "standard",
            "long_context_above_input_tokens": None,
            "models": {
                "gemini-4-neon": {
                    "standard": {
                        "input": 3000,
                        "cached_input": 150,
                        "cache_write": 3000,
                        "output": 15_000,
                    },
                },
            },
        },
    },
    "web_search_call_nanos": 10_000_000,
    "tools": {
        "dataforseo": "provider_reported_task_cost_usd",
        "gak": "provider_reported_task_cost_usd",
        "google_ads": "provider_reported_task_cost_usd",
    },
}

NATIVE_EXECUTORS = {
    "content.plan",
    "creative.character",
    "style.capture",
    "social.x_style",
    "social.x_revise",
    "project.memory",
    "project.weekly_brief",
    "scan.report",
    "visibility.audit",
    "organic.audit",
    "organic.keyword_plan",
    "growth.onboarding_plan",
    "ads.assessment",
    "ads.launch",
    "ads.monitor",
    "revenue.payment_recovery",
}
PARENT_EXECUTORS = {
    "organic.traffic_system",
    "growth.onboarding",
    "social.x_draft",
    "intel.world",
    "intel.daily",
}
# Daily intelligence (intel.world, intel.daily) buys nothing itself: its Antigravity judge is a
# session-funded child with its own $15 ceiling (codex_api_pricing.PROCEDURE_KEY_MAXIMUMS), and
# the child's actual usage is charged once. Observed judge runs cost $2.00 to $2.30; the
# ceiling is a runaway guard, not an estimate.
INTEL_PARENT_USD = 15
# X's pay-per-use price for one post returned by a read, on Tin's X app (2026-11-25). An
# intel.daily run pins it: the client's List is read through Tin's own X connection, and the
# client pays for each page at this price per post X returned (protected posts it counted
# included). A price change is a new id; earlier runs keep theirs.
X_READ_PRICING = {
    "id": "x-pay-per-use-reads-2026-11-25-v1",
    "provider": "x",
    "unit": "post_returned",
    "post_read_nanos": 5_000_000,
}

# Organic audit ceiling, from its bounded calls at the GPT-6 Luna rates above. A run makes at
# most 28 searched calls (two research attempts, 12 questions asked twice, two brand checks) and
# 52 unsearched ones (two question drafts, 24 blind readings, two reviews, 24 judgments), plus
# one crawl reserved at $0.05. Each request's input is capped at 60,000 bytes and its output at
# 6,000 tokens. Counting one token per byte of instructions, input and schema (about 61-65 KB)
# plus 16,384 tokens of results for each of three searches, a searched call costs at most
# $0.047 and an unsearched one $0.011, so even if every call hit every bound at once the run
# would cost $1.92. Real runs cost far less: a production audit, keyword plan and content plan
# together came to $0.73.
AUDIT_MAXIMUM_USD = 2


def audit_maximum_nanos(policy: dict | None) -> int:
    """An audit's own ceiling: organic-audit-v15 pins its own ($4 for sixteen questions)."""
    pinned = amount_nanos((policy or {}).get("billing_maximum_usd"))
    return pinned if pinned is not None else AUDIT_MAXIMUM_USD * NANOS_PER_DOLLAR


# The organic parent's spending pool beyond the founder's keyword limit: about five times what a
# production run spent ($2.49 for audit, keyword research, content plan and one draft, with the
# technical fix and page adaptation costing nothing that run). Every child keeps its own
# ceiling, but each paid call also reserves against the parent's maximum, so the parent's
# maximum is the run's real total. Typical runs finish well inside it; a run whose early
# steps spent unusually much stops its later paid steps rather than exceeding the pool.
TRAFFIC_SYSTEM_POOL_USD = 10
# The content plan's share inside the organic parent. Its one model call reads at most
# 240,000 bytes of evidence plus instructions and schema (about 254,000 tokens) and writes at
# most 32,000 tokens (content-editorial-v8; v7 wrote 16,000): $0.088 even at long-context
# rates, under $0.10. Standalone one-call plans have a $0.25 ceiling (NATIVE_MAXIMUMS_USD).
CONTENT_PLAN_SHARE_USD = 1
# content.plan 1.0.0 (content-editorial-v9) buys no model call itself: its planning agent,
# content.plan_research, is a Codex procedure child with its own $12 ceiling
# (codex_api_pricing.PROCEDURE_MAXIMUMS; $6 before its subagents), so the plan is a spending
# parent of that size, and organic-traffic-v8's ceiling holds it (TRAFFIC_SYSTEM_V8_USD).
CONTENT_PLAN_AGENT_USD = 12


def agent_planner(definition) -> bool:
    """Whether a content.plan definition plans with the agent (v9 and later)."""
    return definition.get("executor") == "content.plan" and bool(
        (definition.get("content_policy") or {}).get("planner")
    )


# Native ceilings below the $2 default, from each workflow's per-call reservations (each call
# reserves its worst case before dispatch) rather than from its p90 (workflow_estimates).
# content.plan's entry is its one-call version; the agent planner is CONTENT_PLAN_AGENT_USD.
# style.capture keeps room for a full sample in a CJK script, whose escaped text reserves about
# $2.01 on its one call; project.memory and weekly_brief keep room for quote-heavy sources.
NATIVE_MAXIMUMS_USD = {
    "content.plan": "0.25",
    "creative.character": "1.50",
    "style.capture": "2.50",
    "social.x_style": "1",
    "social.x_revise": "1",
    "project.memory": "1",
    "project.weekly_brief": "0.50",
    "scan.report": "0.25",
    "visibility.audit": "0.75",
}
# organic-traffic-v8's ceiling beyond the founder's keyword limit: its children's p90s
# (workflow_estimates) come to about $3 without delivery or fixes, and the $12 also holds any
# one child's own ceiling (audit $6, planning agent $12, draft $6, refresh $3). Delivery and
# technical fixes are website.change runs with a $4 ceiling and a p90 under $1, so each adds
# $3; a run that needs more stops its later paid steps.
TRAFFIC_SYSTEM_V8_USD = 12
# Versions before v8 keep the share their first page refresh had when they shipped.
REFRESH_SHARE_BEFORE_V8_NANOS = 2_500_000_000
TRAFFIC_SYSTEM_V8_STEP_USD = 3


# Page decisions' share inside the organic parent (organic-traffic-v7): at most two GPT-6 Luna
# calls of 30,000 input bytes and 2,048 output tokens each, a few cents even at the bound.
PAGE_DECISIONS_SHARE_USD = 1


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
    if executor in NATIVE_MAXIMUMS_USD:
        maximum = amount_nanos(NATIVE_MAXIMUMS_USD[executor])
    kinds = ["native_model"]
    if agent_planner(definition):
        maximum, kinds = CONTENT_PLAN_AGENT_USD * NANOS_PER_DOLLAR, []
    elif executor == "organic.audit":
        maximum = audit_maximum_nanos(definition.get("audit_policy"))
        kinds = ["native_model", "tool"]
        # organic-audit-v13 also asks its questions on six AI engines, within its own pinned
        # ceiling ($1; eight questions cost at most $0.63 at the pinned request prices).
        engines = (definition.get("audit_policy") or {}).get("ai_engines_max_cost_usd")
        if engines is not None:
            maximum += amount_nanos(engines) or 0
    elif executor == "organic.keyword_plan":
        maximum = amount_nanos(inputs.get("max_cost_usd", 2))
        kinds = ["native_model", "tool"]
    elif executor == "organic.traffic_system":
        keywords = amount_nanos(inputs.get("keyword_max_cost_usd", 2))
        maximum = keywords
        version = definition.get("organic_system_policy", {}).get("version")
        if maximum is not None and version == "organic-traffic-v8":
            maximum += (
                TRAFFIC_SYSTEM_V8_USD
                + (TRAFFIC_SYSTEM_V8_STEP_USD if inputs.get("technical_fix") else 0)
                + (
                    TRAFFIC_SYSTEM_V8_STEP_USD
                    if inputs.get("content_delivery", "auto") == "auto"
                    else 0
                )
            ) * NANOS_PER_DOLLAR
        elif maximum is not None:
            maximum += (
                AUDIT_MAXIMUM_USD
                + CONTENT_PLAN_SHARE_USD
                + (5 if inputs.get("technical_fix") else 0)
            ) * NANOS_PER_DOLLAR
            if version in {"organic-traffic-v6", "organic-traffic-v7"}:
                # One draft and, unless explicitly disabled, one repository adaptation.
                # This is a bound, not an upfront charge or six-month reservation. Weekly
                # drafts are ordinary scheduled runs with their own funding.
                maximum += (
                    5 + (5 if inputs.get("content_delivery", "auto") == "auto" else 0)
                ) * NANOS_PER_DOLLAR
            pool = keywords + TRAFFIC_SYSTEM_POOL_USD * NANOS_PER_DOLLAR
            if version in {"organic-traffic-v6", "organic-traffic-v7"}:
                # The first page refresh is a child run; later weekly refreshes are ordinary
                # scheduled runs with their own funding. The pool grows by the refresh's own
                # ceiling, since a production run has not measured one yet. The technical and
                # delivery steps are website.change runs, each with a $5 procedure ceiling.
                # The refresh share stays the $2.50 its ceiling was when these versions shipped.
                maximum += REFRESH_SHARE_BEFORE_V8_NANOS
                pool += REFRESH_SHARE_BEFORE_V8_NANOS
            if version == "organic-traffic-v7":
                # v7's first traffic snapshot (no model call) and Page decisions run as children.
                maximum += PAGE_DECISIONS_SHARE_USD * NANOS_PER_DOLLAR
                pool += PAGE_DECISIONS_SHARE_USD * NANOS_PER_DOLLAR
            # The children's ceilings add up to more than a run spends; the pool bounds the run.
            maximum = min(maximum, pool)
        kinds = []  # The parent itself never buys a model call.
    elif executor == "social.x_draft":
        # One bounded voice capture and one composition; only actual child usage is charged.
        maximum, kinds = 1 * NANOS_PER_DOLLAR, []
    elif executor == "growth.onboarding":
        maximum, kinds = 10 * NANOS_PER_DOLLAR, []
    elif executor == "intel.world":
        maximum, kinds = INTEL_PARENT_USD * NANOS_PER_DOLLAR, []
    elif executor == "intel.daily":
        # The judge is its own session-funded child; the parent buys only X List pages.
        maximum, kinds = INTEL_PARENT_USD * NANOS_PER_DOLLAR, ["tool"]
    elif executor == "growth.onboarding_plan":
        # About twenty-five bounded model steps; each reserves its conservative ceiling first.
        maximum = 8 * NANOS_PER_DOLLAR
    elif executor == "ads.assessment":
        # The founder's ceiling bounds model steps and provider research together.
        maximum = amount_nanos(inputs.get("max_cost_usd", 6))
        kinds = ["native_model", "tool"]
    elif executor == "ads.launch":
        # Model steps only; the Google Ads API reports no cost and is receipted at zero.
        maximum = amount_nanos(inputs.get("max_cost_usd", 4))
        kinds = ["native_model", "tool"]
    elif executor == "ads.monitor":
        maximum = amount_nanos(inputs.get("max_cost_usd", 2))
        kinds = ["native_model", "tool"]
    elif executor == "revenue.payment_recovery":
        # One drafting step; Stripe and Gmail calls go through the founder's own accounts.
        maximum, kinds = 2 * NANOS_PER_DOLLAR, ["native_model", "tool"]
    if type(maximum) is not int or maximum <= 0:
        raise BillingError("invalid_budget", "The workflow spending maximum is invalid.")
    extra = {}
    if executor == "intel.daily":
        # The client's X List pages are billed to its run at X's per-post price.
        extra["x_read_pricing"] = deepcopy(X_READ_PRICING)
    return {
        **extra,
        "rate_card": CARD["id"],
        "service_pricing": deepcopy(CARD),
        "currency": "USD",
        "definition_sha256": digest(definition),
        "kind": "parent"
        if executor in PARENT_EXECUTORS or agent_planner(definition)
        else "metered_workflow",
        "operations": kinds,
        "maximum_nanos": maximum,
        "execution_fee_nanos": 0,
        "rounding": "half_up_cent_at_root_settlement",
        "failure_policy": "verified_usage; platform_duplicates_and_overages_absorbed",
        "unknown_policy": "pending_up_to_24h_then_unresolved_cost_absorbed",
    }


def provider_card(card, provider):
    """The pinned prices for one provider: the card's own, or one of its other providers."""
    if not card:
        return None
    if provider == card["provider"]:
        return card
    return (card.get("other_providers") or {}).get(provider)


def model_card(terms, provider, model):
    card = provider_card(terms.get("service_pricing"), provider)
    if card is None or model not in card["models"]:
        raise BillingError("unpriced_model", "This model has no pinned workflow price.")
    return card, card["models"][model]


def context_band(card, input_tokens):
    threshold = card["long_context_above_input_tokens"]
    return "long_context" if threshold is not None and input_tokens > threshold else "standard"


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
    rate = rates[context_band(card, input_tokens)]
    return (
        input_tokens * max(rate["input"], rate["cache_write"])
        + output_tokens * rate["output"]
        + searches * terms["service_pricing"]["web_search_call_nanos"]
    )


def x_read_charge(pricing, record):
    """One X read at the run's pinned per-post price: the posts X returned, counted by Tin from
    the response it received. None when the receipt doesn't say how many."""
    if record.get("category") != "tool":
        return None
    usage = record.get("usage")
    posts = usage.get("posts_returned") if isinstance(usage, dict) else None
    if type(posts) is not int or not 0 <= posts <= 10_000:
        return None
    return posts * pricing["post_read_nanos"], {
        "provider": "x",
        "endpoint": record.get("endpoint"),
        "basis": "pinned_unit_price",
        "rate_card": pricing["id"],
        "posts_returned": posts,
    }


def price_receipt(terms, kind, record):
    if record.get("outcome") not in {"response_received", "invalid_output"}:
        return None
    card = terms["service_pricing"]
    if kind == "tool" and record.get("provider") == "x" and "x_read_pricing" in terms:
        return x_read_charge(terms["x_read_pricing"], record)
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
    if kind != "native_model":
        return None
    try:
        prices, rates = model_card(terms, record.get("provider"), record.get("model"))
    except BillingError:
        return None
    if record.get("service_tier") != prices["service_tier"]:
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
    band = context_band(prices, inputs)
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
