"""Small USD tariff contract. Supplier expense is a separate measurement."""

import hashlib
import json
import math
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

NANOS_PER_DOLLAR = 1_000_000_000
NANOS_PER_CENT = 10_000_000
Money = Annotated[int, Field(strict=True, ge=0, le=100_000 * NANOS_PER_DOLLAR)]


class BillingError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status

    def diagnostic(self):
        return {"code": self.code, "message": str(self)}


class ProjectSpendingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    per_run_nanos: Money
    monthly_nanos: Money
    schedule_max_nanos: Money | None = None
    expected_revision: Annotated[int, Field(strict=True, ge=0)]


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def object_value(value):
    return json.loads(value) if isinstance(value, str) else dict(value)


def usd(nanos):
    return format(Decimal(nanos) / NANOS_PER_DOLLAR, ".2f")


def usd_nanos(value: str) -> int:
    """Public dollar amounts, never floating point or nanodollar arithmetic for callers."""
    import re

    if not isinstance(value, str) or not re.fullmatch(r"\d{1,6}(?:\.\d{1,2})?", value):
        raise ValueError("Use a USD amount with at most two decimal places, such as 10.00.")
    amount = int(Decimal(value) * NANOS_PER_DOLLAR)
    if not 0 < amount <= 100_000 * NANOS_PER_DOLLAR:
        raise ValueError("USD amount is outside the supported range.")
    return amount


def final_charge(nanos, maximum):
    """Round half-up to cents once per root execution, never per token or child."""
    return min(maximum, ((nanos + NANOS_PER_CENT // 2) // NANOS_PER_CENT) * NANOS_PER_CENT)


def test_terms(definition):
    """Deliberately fictional test tariff, not supplier prices or a live offer.

    Stripe test mode only; BillingService.fallback_terms refuses it in live mode.

    No runtime/model route is selected by this function. All native adapters keep
    their own explicit provider/model contract; only metered executors qualify.
    """
    executor = definition.get("executor")
    if executor in {
        "content.plan",
        "creative.character",
        "style.capture",
        "social.x_style",
        "social.x_revise",
    }:
        kind, maximum = "native_model", 2 * NANOS_PER_DOLLAR
    elif executor == "codex.procedure":
        profile = definition.get("procedure", {}).get("sandbox", {})
        if profile.get("profile") != "isolated":
            raise BillingError("unmetered_profile", "This execution profile is not billing-ready.")
        kind, maximum = "isolated_codex", 10 * NANOS_PER_DOLLAR
    else:
        raise BillingError("unmetered_workflow", "This workflow is not in the paid test pilot.")
    return {
        "rate_card": "tin-test-only-v1",
        "currency": "USD",
        "definition_sha256": digest(definition),
        "kind": kind,
        "maximum_nanos": maximum,
        "execution_fee_nanos": 20_000_000,
        "native_model": {
            "input": 1_000,
            "cached_input": 100,
            "cache_write": 1_250,
            "output": 3_000,
        },
        "isolated_codex": {
            "input": 8_000,
            "cached_input": 800,
            "cache_write": 10_000,
            "output": 40_000,
        },
        "sandbox_second_nanos": 50_000,
        "rounding": "half_up_cent_at_root_settlement",
        "failure_policy": "verified_usage; platform_duplicates_absorbed",
        "unknown_policy": "pending_up_to_24h_then_unresolved_cost_absorbed",
    }


def token_charge(terms, kind, usage):
    names = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens")
    values = [usage.get(name) for name in names]
    if any(type(value) is not int or value < 0 for value in values):
        return None
    inputs, outputs, cached, writes = values
    if cached + writes > inputs:
        return None
    rate = terms[kind]
    return (
        (inputs - cached - writes) * rate["input"]
        + cached * rate["cached_input"]
        + writes * rate["cache_write"]
        + outputs * rate["output"]
    )


def receipt_charge(terms, kind, record):
    """Price only trusted native/isolated receipts, also used after an interrupted write."""
    if kind == "tool" and "studio_pricing" in terms:
        from tin_lite.studio_billing import price

        return price(terms["studio_pricing"], record)
    if "service_pricing" in terms:
        from tin_lite.service_pricing import price_receipt

        return price_receipt(terms, kind, record)
    if kind == "codex_api":
        from tin_lite.codex_api_pricing import price_response

        return price_response(terms.get("pricing"), record)
    usage = record.get("usage")
    if not isinstance(usage, dict):
        return None
    model = record.get("model")
    if kind == "native_model":
        if record.get("outcome") not in {"response_received", "invalid_output"}:
            return None
        usage = dict(usage)
        if record.get("provider") == "openai":
            usage["cache_write_input_tokens"] = 0
        amount = token_charge(terms, kind, usage)
    elif kind == "isolated_codex":
        model = usage.get("model")
        elapsed = record.get("elapsed_seconds")
        if (
            usage.get("final") is not True
            or type(elapsed) not in {float, int}
            or not math.isfinite(elapsed)
            or elapsed < 0
        ):
            return None
        total = usage.get("total") or {}
        usage = {
            "input_tokens": total.get("inputTokens"),
            "cached_input_tokens": total.get("cachedInputTokens"),
            "cache_write_input_tokens": total.get("cacheWriteInputTokens"),
            "output_tokens": total.get("outputTokens"),
        }
        amount = token_charge(terms, kind, usage)
        if amount is not None:
            amount += math.ceil(elapsed * terms["sandbox_second_nanos"])
    else:
        return None
    if amount is None:
        return None
    return amount, {
        "provider": record.get("provider"),
        "model": model,
        "usage": usage,
        "outcome": record.get("outcome"),
        "procedure_seconds": record.get("elapsed_seconds"),
    }
