"""Daily intelligence's billing: parents buy nothing themselves; their judges are session-funded."""

from __future__ import annotations

from tin_lite import codex_api_pricing
from tin_lite.billing_contracts import receipt_charge
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.service_pricing import NANOS_PER_DOLLAR, PARENT_EXECUTORS, service_terms


def definition(key):
    return next(b for b in BUILTIN_WORKFLOWS if b.key == key).definition


def test_the_world_parent_is_a_fifteen_dollar_spending_parent():
    terms = service_terms(definition("intel.world"))
    assert terms["kind"] == "parent" and terms["operations"] == []
    assert terms["maximum_nanos"] == 15 * NANOS_PER_DOLLAR
    assert "external_api_funding" not in terms
    assert {"intel.world", "intel.daily"} <= PARENT_EXECUTORS


def test_the_client_pays_for_its_x_list_pages_at_xs_per_post_price():
    terms = service_terms({"executor": "intel.daily", "key": "intel.daily"})
    assert "external_api_funding" not in terms
    assert terms["kind"] == "parent" and terms["operations"] == ["tool"]
    assert terms["maximum_nanos"] == 15 * NANOS_PER_DOLLAR
    assert terms["x_read_pricing"]["post_read_nanos"] == 5_000_000  # $0.005 a post
    page = {
        "run_id": "r",
        "provider": "x",
        "category": "tool",
        "endpoint": "lists/tweets",
        "outcome": "response_received",
        "usage": {"posts_returned": 100},
    }
    amount, observation = receipt_charge(terms, "tool", page)
    assert amount == NANOS_PER_DOLLAR // 2
    assert observation["basis"] == "pinned_unit_price" and observation["posts_returned"] == 100
    # Unknown outcomes and unreadable counts are never priced, so they stay pending.
    assert receipt_charge(terms, "tool", {**page, "outcome": "unconfirmed"}) is None
    assert receipt_charge(terms, "tool", {**page, "usage": {"posts_returned": "100"}}) is None
    # Other runs' terms don't price X at all.
    world = service_terms(definition("intel.world"))
    assert receipt_charge(world, "tool", page) is None


def test_both_judges_have_a_fifteen_dollar_session_ceiling():
    for key in ("intel.world_judge", "intel.client_judge"):
        assert codex_api_pricing.PROCEDURE_KEY_MAXIMUMS[key] == 15 * NANOS_PER_DOLLAR
