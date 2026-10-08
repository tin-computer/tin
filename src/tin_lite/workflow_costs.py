"""Reusable configured cost policies. No supplier call or per-launch estimation job."""

from functools import lru_cache

from tin_lite.billing_contracts import NANOS_PER_CENT, digest
from tin_lite.workflow_estimates import BASIS, POLICY, estimate_nanos

FUNDING = "per_operation_v1"
SESSION_FUNDING = "procedure_session_v1"
# Estimates before calibrated-p90-v1 were the ceiling itself. Quotes issued under it keep it.
BOUND_POLICY = "configured-cost-bound-v1"


@lru_cache(maxsize=512)
def _estimate(definition_digest, scope_digest, price_digest, maximum, policy, amount):
    # The id names everything the estimate was read from: definition, inputs, prices and the
    # estimate table's policy. Do not learn from another user's runs.
    if policy == BOUND_POLICY:
        return digest([policy, definition_digest, scope_digest, price_digest, maximum])
    return digest([policy, definition_digest, scope_digest, price_digest, maximum, amount])


def configured_terms(terms, definition, inputs, *, policy=POLICY):
    """Terms with their estimate: the calibrated p90 (workflow_estimates), or, for a quote
    issued before it, the configured bound. The ceiling is unchanged either way."""
    if terms["kind"] == "included":
        return terms
    terms = {key: value for key, value in terms.items() if key != "estimate"}
    maximum = terms["maximum_nanos"]
    amount = (
        maximum if policy == BOUND_POLICY else estimate_nanos(definition, inputs or {}, maximum)
    )
    estimate_id = _estimate(
        digest(definition), digest(inputs or {}), digest(terms), maximum, policy, amount
    )
    return {
        **terms,
        "funding": terms.get("funding", FUNDING),
        "estimate": {
            "id": estimate_id,
            "policy": policy,
            "basis": "conservative_configured_bound" if policy == BOUND_POLICY else BASIS,
            "amount_nanos": amount,
        },
    }


def admitted_amount(terms):
    """What admission checks against credits and project limits.

    A per-call run is checked at its estimate; each paid call then reserves against its
    ceiling. A session budget is held whole when the run starts, so it is checked at the
    ceiling it holds.
    """
    maximum = terms["maximum_nanos"]
    if not incremental(terms):
        return maximum
    return terms.get("estimate", {}).get("amount_nanos", maximum)


def incremental(terms):
    return terms.get("funding") == FUNDING


def session_funded(terms):
    return terms.get("funding") == SESSION_FUNDING


def liability(terms, committed):
    """Protect root-level rounding without rounding each individual supplier call."""
    if not incremental(terms):
        return terms["maximum_nanos"]
    amount = committed + (terms["execution_fee_nanos"] if committed else 0)
    return min(
        terms["maximum_nanos"],
        ((amount + NANOS_PER_CENT - 1) // NANOS_PER_CENT) * NANOS_PER_CENT,
    )
